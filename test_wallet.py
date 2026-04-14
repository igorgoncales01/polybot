"""
Teste isolado de conexão com Polymarket CLOB API.
Etapas:
  1. Carregar wallet da .env
  2. Verificar endereço e saldo
  3. Testar auth L1 (derive API key)
  4. Testar auth L2 (HMAC signed request)
  5. Buscar posições abertas
  6. NÃO faz trades — apenas leitura

Uso: python3 test_wallet.py
"""

import os
import sys
import time
import json
import hashlib
import hmac as hmac_mod

# Load .env
from dotenv import load_dotenv
load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"

def banner(msg):
    print(f"\n{'='*50}")
    print(f"  {msg}")
    print(f"{'='*50}")


def test_1_load_wallet():
    """Etapa 1: Carregar wallet"""
    banner("ETAPA 1: Carregar Wallet")

    if not PRIVATE_KEY or PRIVATE_KEY == "0xYOUR_POLYGON_PRIVATE_KEY_HERE":
        print("  ❌ PRIVATE_KEY não configurada no .env")
        print("  → Edite o arquivo .env e coloque sua chave privada Polygon")
        print("  → Formato: PRIVATE_KEY=0x1234abcd...")
        return None

    try:
        from eth_account import Account
        account = Account.from_key(PRIVATE_KEY)
        print(f"  ✅ Wallet carregada")
        print(f"  📍 Endereço: {account.address}")
        print(f"  🔑 Chave: {PRIVATE_KEY[:6]}...{PRIVATE_KEY[-4:]}")
        return account
    except Exception as e:
        print(f"  ❌ Erro ao carregar wallet: {e}")
        return None


def test_2_check_balance(account):
    """Etapa 2: Verificar saldo on-chain"""
    banner("ETAPA 2: Verificar Saldo Polygon")

    try:
        import httpx

        # Polygon RPC - check MATIC balance
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [account.address, "latest"],
            "id": 1
        }
        resp = httpx.post("https://polygon-rpc.com", json=payload, timeout=10)
        data = resp.json()
        matic_wei = int(data["result"], 16)
        matic = matic_wei / 1e18
        print(f"  💜 MATIC: {matic:.4f}")

        # Check USDC balance (Polygon USDC contract)
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon
        # balanceOf(address)
        call_data = f"0x70a08231000000000000000000000000{account.address[2:].lower()}"
        payload2 = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdc_contract, "data": call_data}, "latest"],
            "id": 2
        }
        resp2 = httpx.post("https://polygon-rpc.com", json=payload2, timeout=10)
        data2 = resp2.json()
        usdc_wei = int(data2["result"], 16)
        usdc = usdc_wei / 1e6  # USDC has 6 decimals
        print(f"  💵 USDC: ${usdc:,.2f}")

        # Check USDC.e too (bridged USDC)
        usdce_contract = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
        call_data_e = f"0x70a08231000000000000000000000000{account.address[2:].lower()}"
        payload3 = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdce_contract, "data": call_data_e}, "latest"],
            "id": 3
        }
        resp3 = httpx.post("https://polygon-rpc.com", json=payload3, timeout=10)
        data3 = resp3.json()
        usdce_wei = int(data3["result"], 16)
        usdce = usdce_wei / 1e6
        print(f"  💵 USDC.e: ${usdce:,.2f}")

        if matic < 0.01:
            print(f"  ⚠️  MATIC baixo — precisa de gas pra transações")
        if usdc + usdce == 0:
            print(f"  ⚠️  Sem USDC — precisa depositar pra operar")

        return {"matic": matic, "usdc": usdc, "usdce": usdce}

    except Exception as e:
        print(f"  ❌ Erro ao verificar saldo: {e}")
        return None


def test_3_clob_public():
    """Etapa 3: Testar endpoints públicos do CLOB"""
    banner("ETAPA 3: CLOB API Pública (sem auth)")

    import httpx

    # Server time
    try:
        resp = httpx.get(f"{CLOB_URL}/time", timeout=10)
        print(f"  ✅ CLOB server time: {resp.text[:50]}")
    except Exception as e:
        print(f"  ❌ CLOB unreachable: {e}")
        return False

    # Markets
    try:
        resp = httpx.get(f"{CLOB_URL}/markets?next_cursor=MA==", timeout=10)
        data = resp.json()
        markets = data.get("data", [])
        print(f"  ✅ Markets endpoint: {len(markets)} markets returned")
        if markets:
            m = markets[0]
            print(f"     Sample: {m.get('question', '?')[:50]}")
            print(f"     Token: {m.get('tokens', [{}])[0].get('token_id', '?')[:30]}...")
    except Exception as e:
        print(f"  ❌ Markets error: {e}")
        return False

    # Book
    try:
        if markets and markets[0].get("tokens"):
            token = markets[0]["tokens"][0]["token_id"]
            resp = httpx.get(f"{CLOB_URL}/book?token_id={token}", timeout=10)
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            print(f"  ✅ Orderbook: {len(bids)} bids / {len(asks)} asks")
    except Exception as e:
        print(f"  ❌ Book error: {e}")

    return True


def test_4_derive_api_key(account):
    """Etapa 4: Derive API Key (L1 Auth)"""
    banner("ETAPA 4: Derive API Key (L1 Auth)")

    import httpx
    from eth_account import Account as Acc
    from eth_account.messages import encode_defunct

    try:
        # Build L1 auth signature
        timestamp = int(time.time())
        nonce = 0

        # Polymarket L1 auth message
        msg = f"I want to sign in to Polymarket CLOB. #Timestamp: {timestamp}"
        message = encode_defunct(text=msg)
        signed = account.sign_message(message)
        signature = signed.signature.hex()

        headers = {
            "POLY_ADDRESS": account.address,
            "POLY_SIGNATURE": f"0x{signature}",
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_NONCE": str(nonce),
        }

        print(f"  📝 Address: {account.address}")
        print(f"  📝 Timestamp: {timestamp}")
        print(f"  📝 Signature: 0x{signature[:20]}...")
        print(f"  🔄 Chamando /auth/derive-api-key...")

        resp = httpx.post(
            f"{CLOB_URL}/auth/derive-api-key",
            headers=headers,
            timeout=15,
        )

        print(f"  📨 Status: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            api_key = data.get("apiKey", "")
            secret = data.get("secret", "")
            passphrase = data.get("passphrase", "")
            print(f"  ✅ API Key derivada!")
            print(f"     Key: {api_key[:12]}...")
            print(f"     Secret: {secret[:12]}...")
            print(f"     Passphrase: {passphrase[:12]}...")
            return {"api_key": api_key, "secret": secret, "passphrase": passphrase}
        else:
            print(f"  ❌ Erro: {resp.text[:200]}")

            # Try alternative auth method
            print(f"\n  🔄 Tentando método alternativo (EIP-712)...")
            return test_4b_derive_eip712(account)

    except Exception as e:
        print(f"  ❌ Erro: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_4b_derive_eip712(account):
    """Alternative: derive via py-clob-client style"""
    import httpx

    try:
        timestamp = int(time.time())
        nonce = 0

        # EIP-712 typed data for CLOB auth
        domain = {
            "name": "ClobAuthDomain",
            "version": "1",
            "chainId": 137,
        }
        types = {
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ]
        }
        message = {
            "address": account.address,
            "timestamp": str(timestamp),
            "nonce": nonce,
            "message": "This message attests that I control the given wallet",
        }

        from eth_account.messages import encode_typed_data
        signable = encode_typed_data(
            domain_data=domain,
            message_types={"ClobAuth": types["ClobAuth"]},
            message_data=message,
        )
        signed = account.sign_message(signable)
        signature = signed.signature.hex()

        headers = {
            "POLY_ADDRESS": account.address,
            "POLY_SIGNATURE": f"0x{signature}",
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_NONCE": str(nonce),
        }

        print(f"  📝 EIP-712 Signature: 0x{signature[:20]}...")

        resp = httpx.post(
            f"https://clob.polymarket.com/auth/derive-api-key",
            headers=headers,
            timeout=15,
        )

        print(f"  📨 Status: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            print(f"  ✅ API Key derivada via EIP-712!")
            print(f"     Key: {data.get('apiKey','')[:12]}...")
            return {
                "api_key": data.get("apiKey"),
                "secret": data.get("secret"),
                "passphrase": data.get("passphrase"),
            }
        else:
            print(f"  ❌ Erro: {resp.text[:300]}")
            return None

    except Exception as e:
        print(f"  ❌ Erro EIP-712: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_5_authenticated_request(api_creds):
    """Etapa 5: Fazer request autenticado (L2)"""
    banner("ETAPA 5: Request Autenticado (L2 HMAC)")

    if not api_creds:
        print("  ⏭️  Pulando — sem API key")
        return False

    import httpx

    try:
        api_key = api_creds["api_key"]
        secret = api_creds["secret"]
        passphrase = api_creds["passphrase"]

        # Build HMAC signature
        timestamp = str(int(time.time()))
        method = "GET"
        path = "/auth/api-keys"
        message = f"{timestamp}{method}{path}"

        sig = hmac_mod.new(
            secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        headers = {
            "POLY_ADDRESS": api_creds.get("address", ""),
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": timestamp,
            "POLY_API_KEY": api_key,
            "POLY_PASSPHRASE": passphrase,
        }

        resp = httpx.get(f"{CLOB_URL}/auth/api-keys", headers=headers, timeout=15)

        if resp.status_code == 200:
            keys = resp.json()
            print(f"  ✅ Auth L2 funcionando! {len(keys)} API keys encontradas")
            return True
        else:
            print(f"  ❌ Status {resp.status_code}: {resp.text[:200]}")
            return False

    except Exception as e:
        print(f"  ❌ Erro: {e}")
        return False


def test_6_check_positions(api_creds, account):
    """Etapa 6: Verificar posições na Polymarket"""
    banner("ETAPA 6: Posições na Polymarket")

    import httpx

    try:
        # Public endpoint - check via Gamma API
        resp = httpx.get(
            f"{GAMMA_URL}/positions",
            params={"user": account.address},
            timeout=15,
        )

        if resp.status_code == 200:
            positions = resp.json()
            if positions:
                print(f"  ✅ {len(positions)} posições encontradas")
                for p in positions[:5]:
                    print(f"     {p.get('title', '?')[:50]} | size: {p.get('size', 0)}")
            else:
                print(f"  ℹ️  Nenhuma posição aberta (wallet nova ou vazia)")
        else:
            print(f"  ⚠️  Status {resp.status_code}")

    except Exception as e:
        print(f"  ❌ Erro: {e}")


def test_7_check_allowance(account):
    """Etapa 7: Verificar se USDC está aprovado pro contrato"""
    banner("ETAPA 7: Verificar Allowance (USDC → CTF Exchange)")

    import httpx

    try:
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # CTF Exchange contract on Polygon
        ctf_exchange = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

        # allowance(owner, spender)
        call_data = (
            "0xdd62ed3e"
            f"000000000000000000000000{account.address[2:].lower()}"
            f"000000000000000000000000{ctf_exchange[2:].lower()}"
        )

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdc_contract, "data": call_data}, "latest"],
            "id": 1
        }

        resp = httpx.post("https://polygon-rpc.com", json=payload, timeout=10)
        data = resp.json()
        allowance_wei = int(data["result"], 16)
        allowance = allowance_wei / 1e6

        if allowance > 1_000_000:
            print(f"  ✅ USDC aprovado (unlimited)")
        elif allowance > 0:
            print(f"  ⚠️  USDC aprovado parcial: ${allowance:,.2f}")
            print(f"     → Pode precisar re-aprovar")
        else:
            print(f"  ❌ USDC NÃO aprovado")
            print(f"     → Precisa aprovar antes de operar")
            print(f"     → Faça via site Polymarket ou envie tx approve()")

    except Exception as e:
        print(f"  ❌ Erro: {e}")


# ── Main ──────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🔧 POLYBOT — TESTE DE CONEXÃO COM POLYMARKET")
    print("=" * 50)
    print("  Apenas leitura — NÃO faz trades")
    print("=" * 50)

    # Etapa 1
    account = test_1_load_wallet()
    if not account:
        print("\n⛔ Configure PRIVATE_KEY no .env pra continuar")
        sys.exit(1)

    # Etapa 2
    balances = test_2_check_balance(account)

    # Etapa 3
    clob_ok = test_3_clob_public()
    if not clob_ok:
        print("\n⛔ CLOB API inacessível")
        sys.exit(1)

    # Etapa 4
    api_creds = test_4_derive_api_key(account)
    if api_creds:
        api_creds["address"] = account.address

    # Etapa 5
    test_5_authenticated_request(api_creds)

    # Etapa 6
    test_6_check_positions(api_creds, account)

    # Etapa 7
    test_7_check_allowance(account)

    # Resumo
    banner("RESUMO")
    print(f"  Wallet:    {'✅' if account else '❌'}")
    print(f"  Saldo:     {'✅' if balances and (balances['usdc'] + balances['usdce'] > 0) else '⚠️  Sem USDC'}")
    print(f"  CLOB:      {'✅' if clob_ok else '❌'}")
    print(f"  API Key:   {'✅' if api_creds else '❌'}")
    print(f"  Allowance: verificar acima")
    print()

    if api_creds:
        print("  🟢 PRONTO pra conectar ao bot!")
        print("     Próximo passo: integrar ao executor.py")
    elif account and clob_ok:
        print("  🟡 Wallet OK, CLOB OK, mas auth falhou")
        print("     → Pode ser problema na assinatura, verificar")
    else:
        print("  🔴 Não está pronto")
