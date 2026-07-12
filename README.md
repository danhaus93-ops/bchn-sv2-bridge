# bchn-sv2-bridge

Stratum V2 Template Provider for Bitcoin Cash Node. This is the piece that
did not exist: SV2's template distribution protocol has a Bitcoin Core
implementation but nothing for BCH, which meant no SV2 mining stack could
ever run on Bitcoin Cash. This bridge closes that gap with zero node patches.
Stock BCHN downstream over JSON-RPC, an SRI style pool role upstream over
plaintext SV2 framing.

Status: template server side is live tested against BCH mainnet (first
template pushed at height 959274). Pool role integration is the current
work: SRI pool_sv2 pointed at this bridge, payout configured pool side per
the SV2 spec, testnet4 first.

## What works today

- Full template distribution protocol server: SetupConnection,
  CoinbaseOutputConstraints, NewTemplate, SetNewPrevHash,
  RequestTransactionData, SubmitSolution
- Spec verified field by field against stratum-mining/sv2-spec
- BCH specifics handled: CTOR (full template push only, never incremental),
  no segwit anywhere, witness commitment guard, ABLA sized templates
- Consensus critical path tested: genesis golden vectors, real mainnet
  template validation, full mine and reconstruct loop over TCP
  (test_bridge_e2e.py)
- SubmitSolution gate: local PoW check before submitblock, template prefix
  validation, loud logging on every solution

## Run it

    BCHN_RPC_URL=http://<node>:8332 BCHN_RPC_USER=... BCHN_RPC_PASS=... \
    TP_PORT=8336 python3 bchn_sv2_bridge.py

Stdlib only, no dependencies. Tip detection is best hash polling every 2s;
ZMQ hashblock is the planned upgrade.

## Design constraints that are on purpose

- CTOR means every template refresh is a complete NewTemplate. There is no
  incremental update on BCH and there never will be here.
- Job Declaration is disabled. JD logic that does not know CTOR would
  declare invalid blocks. Solo miners choose the whole template anyway.
- The pool adds payout outputs, not this bridge. That is what the spec says
  and it keeps the bridge stateless about money.

## Roadmap

1. SRI pool role fork configured for BCH, connected to this bridge
2. testnet4 end to end with an SV2 native Bitaxe (ESP-Miner 2.14+)
3. First SV2 block on Bitcoin Cash
4. Ships as part of the LoneStrike Cash stack on Umbrel

LoneStrike Labs. AGPL-3.0.
