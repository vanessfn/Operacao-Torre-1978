# Operação Torre 1978 — CLI 
## Como executar
 
* No terminal Ubuntu, entrar na pasta do projeto e ativar o ambiente virtual:

cd ~/aero70
source .venv/bin/activate

* Dentro do ambiente virtual, os principais comandos são:

python3 torre/torre.py importar-dados
python3 torre/torre.py listar --por=prioridade
python3 torre/torre.py listar --por=voo
python3 torre/torre.py enfileirar decolagem --voo ALT123
python3 torre/torre.py enfileirar pouso --voo ALT901
python3 torre/torre.py autorizar decolagem --pista 10/28
python3 torre/torre.py autorizar pouso --pista 01/19
python3 torre/torre.py status
python3 torre/torre.py relatorio


## Regras implementadas 

* Importação e validação de arquivos de entrada (planos_voo.csv, pistas.txt, frota.csv, pilotos.csv, metar.txt, notam.txt)
* Fila de decolagem e pouso separadas.
* Prioridade de atendimento: emergências > pousos > decolagens; dentro do grupo, segue prioridade e horário.
* Compatibilidade de pista: só autoriza se a aeronave suporta o comprimento da pista e se ela estiver aberta.
* NOTAM ativo: bloqueia pista no horário informado.
* Clima (METAR): se VIS < 6KM, apenas uma operação pode ser autorizada por vez.
* Pilotos: valida vencimento da licença e habilitação compatível com a aeronave.
* Logs: todas as operações são registradas em logs/torre.log.
* Relatório: gera resumo do turno com voos autorizados, negados, motivos e métricas.


## Estrutura de arquivos 

~/aero70/
        |_ dados/
              |_ planos_voo.csv       # Lista de voos (entrada principal)
              |_ pistas.txt            # Situação das pistas
              |_ frota.csv             # Requisitos das aeronaves
              |_ pilotos.csv          # Dados de pilotos
              |_ metar.txt             # Clima (METAR)
              |_ notam.txt            # Ocorrências NOTAM
              |_ fila_decolagem.txt   # Fila dinâmica de decolagens
              |_ fila_pouso.txt           # Fila dinâmica de pousos
      |_ logs/
              |_ torre.log                   # Registro das operações
      |_ relatorios/
              |_operacao_YYYYMMDD.txt  # Relatório diário gerado
      |_ torre/
              |_ torre.py                  # CLI principal
      |_ docs/
              |_ readme.md            # Este documento


## Limitações e próximos passos 
* O sistema ainda não implementa bloqueio de concorrência (file lock) para autorizações simultâneas.
* O horário atual é o do sistema; não há opção de simulação via --hora.
* Possível melhoria: interface mais amigável para listar voos (com tabelas formatadas).
