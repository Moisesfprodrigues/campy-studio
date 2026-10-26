# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-PT/1.1.0/).

## [1.0.0] - 2026-10-26

### Adicionado
- Aplicação renomeada para **CamPy Studio** (`campy_studio.py`), com logótipo, rodapé de créditos e botões coloridos.
- Tirar foto por ADB com foco por toque, ponto de foco escolhido na pré-visualização e atalho `F2`.
- Nomes de ficheiro `AAAAMMDD_HHMMSS.jpg`, sem nunca substituir (sufixos `_1`, `_2`...).
- Descarregar fotos novas ou todas, para `raw/` ou para outra pasta; ignora as que já existem no destino.
- Opção de apagar do telemóvel só depois de verificar a transferência (desligada por omissão e nunca guardada).
- Reabrir a câmara, manter ecrã e câmara sempre ligados, e repor as definições de energia originais.
- Painel de estado (telemóvel, ecrã, câmara, energia) atualizado a cada 5 s.
- Testes automáticos com um telemóvel simulado (`pytest`) e integração contínua no GitHub Actions.

### Alterado
- Registo das fotos descarregadas: `logs_img.txt` (antes `camara_app_baixados.txt`).
- Configuração: `campy_studio_config.json` (antes `camara_app_config.json`).
- Os ficheiros antigos são migrados automaticamente no primeiro arranque.
