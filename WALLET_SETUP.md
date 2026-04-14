# Conexao Wallet - Polymarket CLOB

## Wallet
- Endereco: `0xC6ba0364347777752455e2D6731B52ab2BC5b5D2`
- Rede: Polygon (chain_id: 137)
- Private key: no `.env` (PRIVATE_KEY)

## Saldos
- USDC (PoS bridged): ~$12.54 (token que a Polymarket aceita)
- USDC.e (native): ~$37.87 (NAO aceita na Polymarket)
- POL (MATIC): ~34 (gas)

## Tokens USDC na Polygon (IMPORTANTE)
- USDC (PoS bridged) = `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` ← POLYMARKET USA ESSE
- USDC.e (native) = `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` ← Binance envia esse
- Para converter: Kyber aggregator funciona (parcialmente)

## Contratos Polymarket (Polygon)
- CTF Exchange: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- Neg Risk CTF Exchange: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- Neg Risk Adapter: `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
- USDC aprovado (unlimited) nos 3 contratos ✅

## Auth CLOB API
- L1 (derive key): EIP-712 com domain ClobAuthDomain, GET /auth/derive-api-key
- L2 (HMAC): base64_urlsafe_decode(secret), NÃO incluir query params no path, NÃO incluir POLY_NONCE
- API Key: `b03eab00-cce1-f8e3-7060-59b11649f8ed`

## SDK
- Python: `~/miniconda3/bin/python3` (3.13)
- Pacote: `py-clob-client` (0.34.6) instalado no miniconda
- Python do sistema (3.9.6) NAO suporta py-clob-client

## Teste realizado
- Ordem BUY 1000 shares Dallas Stars @ 0.5c colocada e cancelada com sucesso
- Order ID: `0x6f36998d0f6be819c4c6c22c17bf19617268969a8f79682d00681bbc8af6ee44`

## Como rodar com SDK
```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

client = ClobClient('https://clob.polymarket.com', key=PRIVATE_KEY, chain_id=137)
client.set_api_creds(client.create_or_derive_api_creds())

# Criar e postar ordem
order_args = OrderArgs(price=0.005, size=1000, side='BUY', token_id=TOKEN_ID)
signed = client.create_order(order_args)
result = client.post_order(signed, OrderType.GTC)

# Cancelar
client.cancel(order_id)
client.cancel_all()
```

## Problemas encontrados
1. Binance envia USDC.e (native), Polymarket aceita USDC (PoS bridged) - tokens diferentes
2. Python 3.9.6 nao instala py-clob-client - precisou miniconda com Python 3.13
3. L2 HMAC: nao incluir POLY_NONCE no header, nao incluir query params no path do HMAC
4. Approves on-chain precisam ser feitos no USDC correto (0x2791) e depois chamar update_balance_allowance via API
5. Order payload: salt como INT, signatureType como INT, side como STRING ("BUY"/"SELL"), signature DENTRO do order
