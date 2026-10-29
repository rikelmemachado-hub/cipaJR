# Eleição digital da CIPA

Aplicação local com servidor Python e banco SQLite. Os arquivos HTML não devem ser abertos diretamente: login, cadastros, apuração e controle de voto dependem do servidor.

## Executar no Windows

1. Instale Python 3.12 ou superior.
2. Abra o PowerShell nesta pasta e execute `.\start.ps1`.
3. Informe o e-mail do administrador e crie uma senha com pelo menos 14 caracteres. Se `admin.local.xml` existir, o script carregará a credencial local protegida pelo Windows sem solicitá-la novamente.
4. Acesse `http://localhost:3000`.

O servidor pede a senha sem mostrá-la na tela. Ele usa apenas bibliotecas incluídas no Python e cria `data/election.sqlite3` para armazenar cadastros, votos e o histórico de eleições.

## Preparar e realizar uma votação

1. Entre como administrador.
2. Cadastre os eleitores individualmente. O código de cada eleitor é exibido uma única vez; entregue-o por um canal privado.
3. Cadastre as candidaturas e confira os dados.
4. Abra a votação. Os cadastros ficam bloqueados.
5. Cada eleitor acessa a urna com matrícula e código individual, confirma uma escolha e recebe um protocolo.
6. Encerre a votação. O encerramento é definitivo; a apuração só fica disponível depois dele.
7. Para outra eleição, clique em **Iniciar nova eleição**. A nova eleição começa sem funcionários ou candidatos cadastrados; os resultados anteriores ficam preservados na seção **Histórico de eleições**. É possível cadastrar novamente as mesmas matrículas em uma nova eleição.

As telas incluem seções expansíveis **Ajuda do administrador** e **Ajuda do eleitor**, também disponíveis na página da urna, com instruções de uso.

O servidor registra separadamente o fato de que cada eleitor votou e a cédula anônima, sem gravar a matrícula ou o identificador do eleitor na tabela de votos. Isso reduz a associação entre identidade e escolha, mas não constitui uma auditoria independente nem prova criptográfica de sigilo.

## Testes

```powershell
$pythonDirectory = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory -Filter "Python*" | Sort-Object Name -Descending | Select-Object -First 1
& (Join-Path $pythonDirectory.FullName "python.exe") -m unittest discover -s tests -v
```

## Limites antes de uma eleição oficial

Esta versão serve para desenvolvimento e validação do fluxo. O servidor HTTP incluído é simples, não substitui uma configuração de produção, e a aplicação não certifica conformidade legal ou eleitoral. Antes de uso oficial, obtenha aprovação da comissão eleitoral e assessoria responsável; avalie requisitos de CIPA, privacidade/LGPD, acessibilidade, auditoria independente, retenção e cópias de segurança; publique atrás de HTTPS em infraestrutura gerenciada; configure `CIPA_PUBLIC_ORIGIN` e `CIPA_COOKIE_SECURE=true`; proteja o banco e credenciais; e teste restauração e carga. Não exponha o servidor local à internet nem use dados reais antes dessas medidas.

Configuração opcional: `CIPA_HOST`, `CIPA_PORT` e `CIPA_DATABASE`. Por padrão, o servidor escuta apenas no computador local, na porta `3000`.

No Windows, `admin.local.xml` é uma credencial protegida pelo DPAPI do usuário que a criou e ignorada pelo Git. Ela só pode ser reutilizada pela mesma conta do Windows neste computador; não a compartilhe nem a copie para outro equipamento.

Redefinir administrador localmente (opcional)

Por segurança não existe senha padrão: o e‑mail e a senha do administrador são definidos ao iniciar o servidor (`start.ps1`). Se você esqueceu a senha e precisa redefini‑la sem alterar o banco de dados, existe uma rota local temporária que pode ser habilitada para este fim.

1. Defina a variável de ambiente `CIPA_ALLOW_LOCAL_ADMIN_RESET=1` antes de iniciar o servidor. Exemplo no PowerShell:

```powershell
$env:CIPA_ALLOW_LOCAL_ADMIN_RESET = '1'
.\start.ps1
```

2. Faça um POST local (somente do próprio computador) para `http://localhost:3000/api/admin/reset-local` com JSON contendo o novo e‑mail e a nova senha (mínimo 14 caracteres). Exemplo com curl:

```powershell
curl -X POST http://localhost:3000/api/admin/reset-local -H "Content-Type: application/json" -d "{\"email\":\"novo@exemplo.com\",\"password\":\"uma-senha-segura-de-14+\"}"
```

Observações:
- A rota só responde a requisições originadas do próprio host (127.0.0.1 ou ::1) e só funciona enquanto `CIPA_ALLOW_LOCAL_ADMIN_RESET=1` estiver ativo no processo do servidor.
- A alteração é mantida apenas em memória pelo processo em execução; ao reiniciar o servidor será necessário fornecer novamente as variáveis de ambiente ou usar `start.ps1` para definir credenciais interativamente.
- Use essa rota apenas em ambiente de desenvolvimento/local; não a habilite em servidores expostos à Internet.
