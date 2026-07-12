#!/usr/bin/env python3
"""test_bridge_e2e.py — full-loop integration test over real TCP.
FakeNode serves a regtest-difficulty template; a mock pool client performs the
complete TDP conversation: SetupConnection -> constraints -> NewTemplate +
SetNewPrevHash -> RequestTransactionData -> grind nonce against the target
FROM THE WIRE -> SubmitSolution -> FakeNode's submitblock independently
re-verifies the block. Then a fake new tip forces a template rotation.
"""
import asyncio, struct, sys
import bch_tp_core as core
import sv2_framing as sv2
from bchn_sv2_bridge import Bridge, target_le32_from_nbits

FAKE_TX = ("01000000010000000000000000000000000000000000000000000000000000000"
           "000000000ffffffff0451515151ffffffff0100f2052a010000000451515151"
           "00000000")

class FakeNode:
    def __init__(self):
        self.height = 959268
        self.prev = "11" * 32
        self.submitted = []
    def _gbt(self):
        txid = core.b2h_be(core.dsha(bytes.fromhex(FAKE_TX)))
        return {"version": 0x20000000, "height": self.height,
                "previousblockhash": self.prev, "curtime": 1752170000,
                "bits": "207fffff", "coinbasevalue": 312606806,
                "transactions": [{"txid": txid, "data": FAKE_TX}]}
    async def get_template(self): return self._gbt()
    async def best_hash(self): return self.prev
    async def submit_block(self, hexblock):
        blk = bytes.fromhex(hexblock)
        hdr = blk[:80]
        # independent verification: PoW + merkle + body txs
        nbits = struct.unpack("<I", hdr[72:76])[0]
        assert core.pow_ok(hdr, nbits), "submitted block fails PoW"
        assert hdr[4:36] == core.h2b_le(self.prev), "wrong prev hash"
        cb_end = blk.find(bytes.fromhex(FAKE_TX), 81)
        assert cb_end > 81, "coinbase/tx layout broken"
        cb = blk[81:cb_end]
        # scriptSig must begin with the BIP34 height push for this height
        off = 4 + 1 + 32 + 4
        sslen = cb[off]
        import bch_tp_core as c2
        want = c2.bip34_height_push(self.height)
        assert cb[off+1:off+1+len(want)] == want, "BIP34 prefix missing"
        txid_le = core.dsha(bytes.fromhex(FAKE_TX))
        assert core.merkle_root([core.dsha(cb), txid_le]) == hdr[36:68], \
            "merkle mismatch"
        self.submitted.append(hexblock)
        return None   # bitcoind convention: None == accepted

async def read_frame(reader):
    hdr = await reader.readexactly(6)
    mtype = hdr[2]
    ln = struct.unpack("<I", hdr[3:6] + b"\x00")[0]
    return mtype, (await reader.readexactly(ln) if ln else b"")

async def mock_pool(port, results):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    # 1. handshake
    writer.write(sv2.build_setup_connection(host="127.0.0.1", port=port))
    await writer.drain()
    mtype, p = await read_frame(reader)
    assert mtype == sv2.MSG_SETUP_CONNECTION_SUCCESS, hex(mtype)
    assert sv2.parse_setup_success(p)["used_version"] == 2
    results.append("handshake")
    # SPEC: nothing should arrive until we declare constraints
    try:
        await asyncio.wait_for(reader.readexactly(1), timeout=0.4)
        raise SystemExit("server sent data before CoinbaseOutputConstraints")
    except asyncio.TimeoutError:
        results.append("spec-quiet-until-constraints")
    writer.write(sv2.build_coinbase_output_constraints(2048, 400))
    await writer.drain()
    mtype, p = await read_frame(reader)
    assert mtype == sv2.MSG_NEW_TEMPLATE, hex(mtype)
    t = sv2.parse_new_template(p)
    assert t["future_template"] is True, "constraints reply must be future"
    assert t["coinbase_tx_outputs"] == b"" and t["coinbase_tx_outputs_count"] == 0
    assert len(t["coinbase_prefix"]) <= 8
    mtype, p = await read_frame(reader)
    assert mtype == sv2.MSG_SET_NEW_PREV_HASH, hex(mtype)
    pv = sv2.parse_set_new_prev_hash(p)
    assert pv["template_id"] == t["template_id"]
    results.append("template")
    # 4. request tx data, verify round-trip
    writer.write(sv2.build_request_tx_data(t["template_id"])); await writer.drain()
    mtype, p = await read_frame(reader)
    assert mtype == sv2.MSG_REQUEST_TX_DATA_SUCCESS, hex(mtype)
    txd = sv2.parse_request_tx_data_success(p)
    assert txd["transaction_list"] == [bytes.fromhex(FAKE_TX)]
    results.append("txdata")
    # 5. mine it: rebuild coinbase from prefix + extranonce, fold merkle path,
    #    grind nonce against the TARGET FROM THE WIRE (not from local nbits)
    # SPEC: pool constructs the FULL coinbase itself: scriptSig begins with
    # template prefix, pool adds its own extranonce bytes + payout output;
    # template outputs (empty on BCH) appended verbatim at the end.
    ssig = bytes([len(t["coinbase_prefix"])]) + t["coinbase_prefix"] + bytes(range(8))
    payout = (core.varint(1)
              + struct.pack("<q", t["coinbase_tx_value_remaining"])
              + core.varint(25) + b"\x76\xa9\x14" + b"\x11"*20 + b"\x88\xac")
    cb = (struct.pack("<i", t["coinbase_tx_version"]) + core.varint(1)
          + b"\x00"*32 + b"\xff"*4 + core.varint(len(ssig)) + ssig
          + struct.pack("<I", t["coinbase_tx_input_sequence"])
          + payout + t["coinbase_tx_outputs"]
          + struct.pack("<I", t["coinbase_tx_locktime"]))
    cb_txid = core.dsha(cb)
    root = cb_txid
    for sib in t["merkle_path"]:
        root = core.dsha(root + sib)
    target = int.from_bytes(pv["target"], "little")
    nonce = None
    for n in range(500000):
        hdr = (struct.pack("<i", t["version"]) + pv["prev_hash"] + root
               + struct.pack("<I", pv["header_timestamp"])
               + struct.pack("<I", pv["nbits"]) + struct.pack("<I", n))
        if int.from_bytes(core.dsha(hdr), "little") <= target:
            nonce = n; break
    assert nonce is not None, "no nonce found at regtest difficulty"
    results.append(f"mined nonce={nonce}")
    # 6. submit the FULL coinbase (spec) and expect the bridge to accept
    writer.write(sv2.build_submit_solution(
        t["template_id"], t["version"], pv["header_timestamp"], nonce, cb))
    await writer.drain()
    await asyncio.sleep(0.3)
    results.append("solution-sent")
    # 7. rotation: node announces a new tip; expect fresh NewTemplate+prevhash
    mtype, p = await read_frame(reader)
    assert mtype == sv2.MSG_NEW_TEMPLATE, hex(mtype)
    t2 = sv2.parse_new_template(p)
    assert t2["template_id"] != t["template_id"]
    assert t2["future_template"] is True, "new-block template must be future"
    mtype, p = await read_frame(reader)
    assert mtype == sv2.MSG_SET_NEW_PREV_HASH
    pv2 = sv2.parse_set_new_prev_hash(p)
    assert pv2["prev_hash"] != pv["prev_hash"]
    results.append("rotation")
    writer.close()

async def main():
    node = FakeNode()
    bridge = Bridge(node, log=lambda *a: print(*a))
    server = await asyncio.start_server(bridge.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    results = []
    pool_task = asyncio.create_task(mock_pool(port, results))
    await asyncio.sleep(0.6)             # let handshake+solution complete
    assert node.submitted, "no block was submitted"
    # trigger rotation AFTER solution landed
    node.height += 1
    node.prev = "22" * 32
    for w in list(bridge.clients):
        pass
    await bridge.push_template("new-block")
    await asyncio.wait_for(pool_task, timeout=10)
    server.close(); await server.wait_closed()
    print()
    print("E2E steps:", " -> ".join(results))
    print(f"blocks accepted by fake node: {len(node.submitted)}")
    assert results[-1] == "rotation"
    print("E2E INTEGRATION TEST PASSED")

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
