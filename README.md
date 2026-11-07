# Rua Segura (MVP)

Aplicação web para cidadãos sinalizarem buracos, semáforos queimados e outros obstáculos em ruas, calçadas e ciclovias, com um painel para a prefeitura acompanhar e resolver as ocorrências.

## Como rodar

Requer apenas Python 3.9 ou mais recente (nenhuma biblioteca extra).

```powershell
cd rua_segura
$env:ADMIN_PIN = "escolha-um-pin"   # opcional; sem isso, um PIN temporário aparece no terminal
python app.py
```

- Cidadão: http://localhost:8000
- Prefeitura: http://localhost:8000/admin (entre com o PIN)

Para testar no celular na mesma rede Wi-Fi: `$env:HOST = "0.0.0.0"` antes de rodar e acesse `http://<IP-do-computador>:8000`.
O botão "Usar minha localização" só funciona em `localhost` ou HTTPS; pelo IP da rede, marque o local tocando no mapa.

Para mudar a cidade inicial do mapa: `$env:MAP_LAT = "-19.9167"; $env:MAP_LNG = "-43.9345"` (exemplo: Belo Horizonte).

## Funcionalidades

**Cidadão:** marca o local no mapa (ou usa o GPS), escolhe o tipo (buraco, semáforo, iluminação, bueiro, sinalização, obstáculo na ciclovia, árvore caída, alagamento, outro) e onde fica (rua/ciclovia/calçada), adiciona uma descrição e uma foto opcionais, e vê as ocorrências já registradas no mapa coloridas por status.

**Prefeitura:** mapa e lista de ocorrências, contadores por status, filtros, alteração do status (Aberto, Em andamento, Resolvido), observação interna, visualização da foto, link para o OpenStreetMap e exportação em CSV (abre no Excel).

## Privacidade

- O cidadão não se identifica: não há cadastro, nome ou e-mail.
- A foto é reduzida no navegador e os metadados (incluindo o GPS do aparelho) são removidos antes do envio.
- O mapa público mostra apenas tipo, status e data. A descrição, a foto e as observações ficam visíveis só no painel com PIN.
- Os logs do servidor não registram o endereço IP.

## Limitações conhecidas do MVP

- Autenticação por um PIN único e compartilhado, sem usuários individuais nem limite de tentativas.
- Sem HTTPS próprio: para publicar na internet, coloque o app atrás de um proxy com HTTPS.
- Sem moderação nem proteção contra envio em massa (spam).
- Dados em um único arquivo SQLite (`ocorrencias.db`).
- Os mapas usam os servidores públicos do OpenStreetMap, adequados apenas para uso de baixo volume.
