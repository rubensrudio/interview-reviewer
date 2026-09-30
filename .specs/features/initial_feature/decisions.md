# Decisões — initial_feature (Interview Reviewer MVP)

Etapa de todas as decisões abaixo: gate de lacunas do `spec-writer` (Passo 2 do `/generate-plan`).
LAC-01, LAC-02, LAC-08 e LAC-14 foram perguntadas individualmente. As demais foram apresentadas em lote, com a recomendação de cada uma, e o humano aceitou todas as recomendações.

## Decisões do humano

### LAC-01 — Onde a LLM "local" roda?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) servidor ou infra privada do projeto, acessada só pelo backend B) na máquina do usuário C) híbrido
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: pergunta individual.

### LAC-02 — O que "a LLM deve ser treinada" exige no MVP?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) sem ajuste de pesos: modelo base + instruções + recuperação + conjunto de validação B) ajuste fino com dataset curado e autorizado, comparado ao modelo base C) treinar só a extração de skills
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: pergunta individual. Ajuste fino fica como evolução se A não atingir as metas.

### LAC-03 — Quais metas quantitativas aprovam o modelo?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) cliente define agora B) MVP entrega processo e relatório de validação; metas aprovadas antes da escolha final do modelo, como gate de release C) sem metas
- **Recomendação do spec-writer**: B
- **Escolha**: B
- **Observações do humano**: aceita em lote.

### LAC-04 — Aprovar rubrica 0-4, corte satisfatório em 3, pesos iguais e percentual só sobre obrigatórias (RN04-RN06)?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) aprovar como proposto B) 0-4 com peso por nível ou importância da skill C) outra escala ou corte
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-05 — Vaga com mais de 20 skills obrigatórias distintas?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) impedir o início até o usuário revisar e reduzir ou separar a lista B) sistema propõe agrupamento e usuário confirma C) usuário escolhe quais ficam de fora e o relatório as lista como não avaliadas
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-06 — Total padrão N de perguntas e quem define (M ≤ N ≤ 20)?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) N = M fixo B) sistema propõe M + extras até 20, usuário confirma C) usuário escolhe N entre M e 20
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-07 — Requisitos não técnicos da vaga (soft skills, idioma, anos de experiência)?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) listar como "não avaliados nesta sessão", fora do plano e do percentual B) perguntas comportamentais fora do percentual C) ignorar sem mencionar
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-08 — Idioma da entrevista e idiomas de CV/vaga suportados no MVP?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) só inglês B) PT e EN, usuário escolhe ao iniciar C) PT e EN, segue o idioma da vaga
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: pergunta individual. RF29 (P2) amplia idiomas depois.

### LAC-09 — Como obter o conhecimento técnico da web?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) base curada, coletada antes a partir de fontes aprovadas, atualizada periodicamente, sem busca durante a sessão B) busca controlada durante a preparação de cada sessão C) combinação: base + busca sob demanda
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-10 — Sem fonte técnica suficiente para uma skill?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) gerar a pergunta e marcar "sem fonte verificada" no plano e no relatório B) impedir o início e pedir revisão da skill C) gerar a pergunta, mas a skill fica fora do percentual
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-11 — Excluir currículo: o que acontece com os retratos em sessões e relatórios?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) apaga arquivo, texto e extração; sessões mantêm só o retrato mínimo (skills e evidências citadas) até o usuário excluir a sessão B) apaga em cascata sessões e relatórios, com aviso C) apaga o arquivo; sessões mantêm retrato completo
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote. Usuário é avisado antes de confirmar.

### LAC-12 — Excluir conta: prazo, carência e backups?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) exclusão imediata e definitiva após confirmação; cópias em backup expiram no ciclo de retenção (até 30 dias) B) carência de 30 dias, depois exclusão definitiva C) desativação, exclusão manual pelo operador
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote. Prazo de backup declarado ao usuário.

### LAC-13 — Consentimento e uso de dados em treinamento?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) aceite de termos e política no cadastro; dados de usuário nunca usados em treinamento no MVP B) aceite + opt-in explícito para treinamento C) sem aceite formal
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-14 — Existe perfil de operador técnico com interface no MVP?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) não: modelo e fontes operados via configuração ou infra, sem tela e sem acesso a dados de usuário B) sim: tela de fontes, versão do modelo e métricas sem conteúdo pessoal C) sim, com acesso a conteúdo de usuário para suporte, com registro
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: pergunta individual.

### LAC-15 — Login Google com o mesmo e-mail de uma conta local: como comprovar controle e vincular?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) pedir a senha da conta local e vincular em seguida B) enviar link de confirmação ao e-mail local C) recusar o login Google e orientar a usar senha
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-16 — Conta local não verificada pode usar o sistema?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) não entra até verificar B) entra, mas não envia CV nem inicia sessão C) uso pleno com lembrete
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-17 — Limite de tamanho do PDF e de versões de currículo por usuário?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) 5 MB e até 10 versões B) 10 MB, sem limite de versões C) 2 MB e até 5 versões
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote. Valores configuráveis.

### LAC-18 — Quando uma sessão é abandonada ou cancelada?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) só por cancelamento explícito B) cancelamento explícito + expiração após 30 dias sem atividade C) expira em 24 h
- **Recomendação do spec-writer**: B
- **Escolha**: B
- **Observações do humano**: aceita em lote.

### LAC-19 — Avaliação final falha depois das tentativas automáticas?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) estado "falha na avaliação", respostas preservadas, usuário pode pedir nova tentativa B) só o operador reprocessa C) tentativas indefinidas em segundo plano
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-20 — Quantas sessões não concluídas simultâneas por usuário?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) uma por vez B) sem limite C) até 3
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-21 — O que o usuário corrige na extração do CV e como a correção fica marcada?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) adicionar, editar e remover skills, experiências e formação; alterados marcados "informado pelo usuário" sem evidência do texto B) só remover ou renomear C) só editar skills
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-22 — Escala do nível esperado por skill da vaga?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) júnior / pleno / sênior / especialista, opcional B) texto livre C) 1 a 5
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-23 — Metas de desempenho, volume e disponibilidade no MVP?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) sem meta numérica; medir e exibir status de tarefas longas; metas depois da escolha do hardware B) metas agora (ex.: pergunta em até 30 s, 20 usuários simultâneos) C) metas por tipo de operação definidas pelo cliente
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-24 — Política de senha da conta local?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) mínimo 8 caracteres + bloqueio de senhas comuns B) mínimo 12 caracteres C) regras de composição
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-26 — Usuário pode pedir reavaliação de relatório concluído no MVP?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) não; relatório concluído é imutável B) sim, gera resultado separado, identificado pela versão do modelo
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote.

### LAC-27 — Tamanho máximo de uma resposta no chat?
- **Data**: 2026-09-30
- **Etapa**: spec-writer (LACUNAS)
- **Opções apresentadas**: A) 5.000 caracteres B) 10.000 C) sem limite
- **Recomendação do spec-writer**: A
- **Escolha**: A
- **Observações do humano**: aceita em lote. Valor configurável.

## Premissas assumidas (lacunas não bloqueantes)

### LAC-25 — Texto exato das mensagens ao usuário
- **Premissa**: o spec propõe os textos das mensagens em inglês (seção de mensagens), revisáveis.
- **Reversibilidade**: alta
- **Onde impacta**: spec.md (mensagens ao usuário), telas do frontend
- **Jev**: muda_construcao=0.08, area_sensivel=0.04, permissao=0.03, erro_critico=0.03, sem_criterio=0.25

### Premissas já presentes na descrição
- **Premissa**: RN03 (resposta aceita não muda; mudar exige nova sessão); "Não sei" conta como resposta; mensagem vazia rejeitada; reenvio idempotente.
- **Reversibilidade**: média
- **Onde impacta**: spec.md (INTV), contrato de envio de resposta

### Premissa sem LAC — Nível-alvo de acessibilidade (RNF07)
- **Premissa**: WCAG 2.1 AA como nível-alvo do RNF07.
- **Reversibilidade**: alta
- **Onde impacta**: spec.md seção 10, critérios de acessibilidade do frontend

### Premissa sem LAC — CV e vaga fora do inglês (interpretação de LAC-08)
- **Premissa**: CV ou texto de vaga em idioma diferente de inglês é rejeitado com mensagem explicativa (Jev: rejeitar p=0.99).
- **Reversibilidade**: média (RF29/LANG amplia depois)
- **Onde impacta**: spec.md CV-14, PLAN-15
