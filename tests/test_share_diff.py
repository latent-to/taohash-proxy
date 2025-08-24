import hashlib
import th_o3
import th_proxy.protocol.difficulty as py_diff
import random
import th_proxy

EXAMPLE_BLOCK = {
    "id": "00000000000000000001ced364d101a671454761ea9f61656049b04dd92f5221",
    "height": 911248,
    "version": 653344768,
    "timestamp": 1755911586,
    "bits": 386018193,
    "nonce": 510877556,
    "difficulty": 129699156960680.9,
    "merkle_root": "c8ae11888cb8a97fe3bed11c5e06cb91e1b7c6ba908d2e5067a03ca1c1c71043",
    "tx_count": 3308,
    "size": 1552870,
    "weight": 3996979,
    "previousblockhash": "00000000000000000001784a63d4d61afded06173f8e2a221faf747424179ad8",
    "mediantime": 1755905992,
    "stale": False,
    "extras": {
        "reward": 318825116,
        "coinbaseRaw": "0390e70d04a315a9687c204d415241204d61646520696e2055534120f09f87baf09f87b8207c763034fabe6d6da70508e88181ed58db1e896d2e501d6ab0ee4d7c72543999b07dbb31c2f43cc0010000000000000013a6243ef50000947a19ffffffff",
        "orphans": [],
        "medianFee": 4.9883392463256895,
        "feeRange": [
            3.732394366197183,
            4,
            4,
            4.763257575757576,
            5.84070796460177,
            8.03968253968254,
            200,
        ],
        "totalFees": 6325116,
        "avgFee": 1912,
        "avgFeeRate": 6,
        "utxoSetChange": 2915,
        "avgTxSize": 469.32,
        "totalInputs": 7507,
        "totalOutputs": 10422,
        "totalOutputAmt": 1252888843101,
        "segwitTotalTxs": 3051,
        "segwitTotalSize": 1423872,
        "segwitTotalWeight": 3481095,
        "feePercentiles": None,
        "virtualSize": 999244.75,
        "coinbaseAddress": "bc1q695z03z6kweljcvpwft7vfu6kd0guf24yaaht2",
        "coinbaseAddresses": ["bc1q695z03z6kweljcvpwft7vfu6kd0guf24yaaht2"],
        "coinbaseSignature": "OP_0 OP_PUSHBYTES_20 d16827c45ab3b3f961817257e6279ab35e8e2555",
        "coinbaseSignatureAscii": "\x03\x90ç\r\x04£\x15©h| MARA Made in USA ð\x9f\x87ºð\x9f\x87¸ |v04ú¾mm§\x05\x08è\x81\x81íXÛ\x1e\x89m.P\x1dj°îM|rT9\x99°}»1Âô<À\x01\x00\x00\x00\x00\x00\x00\x00\x13¦$>õ\x00\x00\x94z\x19ÿÿÿÿ",
        "header": "0040f126d89a17247474af1f222a8e3f1706edfd1ad6d4634a78010000000000000000004310c7c1a13ca067502e8d90bac6b7e191cb065e1cd1bee37fa9b88c8811aec8a215a968912b0217745f731e",
        "utxoSetSize": None,
        "totalInputAmt": None,
        "pool": {
            "id": 115,
            "name": "MARA Pool",
            "slug": "marapool",
            "minerNames": None,
        },
        "matchRate": 99.91,
        "expectedFees": 6336089,
        "expectedWeight": 3991979,
        "similarity": 0.992190190304005,
    },
}


def test_build_block_header():
    ntime = EXAMPLE_BLOCK["timestamp"].to_bytes(4, 'big').hex()
    nonce = EXAMPLE_BLOCK["nonce"].to_bytes(4, 'big').hex()
    version = EXAMPLE_BLOCK["version"].to_bytes(4, 'big').hex()
    nbits = EXAMPLE_BLOCK["bits"].to_bytes(4, 'big').hex()
    
    merkle_root = int.from_bytes(bytes.fromhex(EXAMPLE_BLOCK["merkle_root"]), 'little').to_bytes(32, 'big').hex()
    prevhash = int.from_bytes(bytes.fromhex(EXAMPLE_BLOCK["previousblockhash"]), 'little').to_bytes(32, 'big').hex()

    block_header = th_o3.protocol.difficulty.get_block_header(
        version,
        prevhash,
        merkle_root,
        ntime,
        nbits,
        nonce,
    )
    py_block_header = py_diff.get_block_header(
        version,
        prevhash,
        merkle_root,
        ntime,
        nbits,
        nonce,
    )

    assert block_header.hex() == py_block_header.hex()
    assert block_header.hex() == EXAMPLE_BLOCK["extras"]["header"]

def test_calculate_block_hash():
    ntime = EXAMPLE_BLOCK["timestamp"].to_bytes(4, 'big').hex()
    nonce = EXAMPLE_BLOCK["nonce"].to_bytes(4, 'big').hex()
    version = EXAMPLE_BLOCK["version"].to_bytes(4, 'big').hex()
    nbits = EXAMPLE_BLOCK["bits"].to_bytes(4, 'big').hex()
    
    merkle_root = int.from_bytes(bytes.fromhex(EXAMPLE_BLOCK["merkle_root"]), 'little').to_bytes(32, 'big').hex()
    prevhash = int.from_bytes(bytes.fromhex(EXAMPLE_BLOCK["previousblockhash"]), 'little').to_bytes(32, 'big').hex()

    block_header = th_o3.protocol.difficulty.get_block_header(
        version,
        prevhash,
        merkle_root,
        ntime,
        nbits,
        nonce,
    )
    
    block_hash = hashlib.sha256(hashlib.sha256(block_header).digest()).digest()
    bh_real = bytes.fromhex(EXAMPLE_BLOCK["id"])

    bh_num = int.from_bytes(block_hash, 'little')
    bh_bytes_be = bh_num.to_bytes(32, 'big')
    assert bh_bytes_be == bh_real

def test_calculate_diff_from_hash():
    hash_hex = EXAMPLE_BLOCK["id"]
    diff = th_o3.protocol.difficulty.get_difficulty(hash_hex)
    py_diff_ = py_diff.get_difficulty(hash_hex)

    #assert diff == EXAMPLE_BLOCK["difficulty"]
    assert py_diff_ == diff
    assert diff >= EXAMPLE_BLOCK["difficulty"]

    test_h = "0115f239049a7574f8ac2088bb26998de311c1fdd4bcae636d7eafae02970f36"
    diff2 = th_o3.protocol.difficulty.get_difficulty(test_h)
    print("diff2:", diff2)

def test_get_merkle_root():
    coinbase_raw = EXAMPLE_BLOCK["extras"]["coinbaseRaw"]
    merkle_branches = [EXAMPLE_BLOCK["merkle_root"], f"{random.randint(0, 2**32 - 1):064x}"]

    py_merkle_root = py_diff.get_merkle_root(coinbase_raw, merkle_branches)
    merkle_root = th_o3.protocol.difficulty.get_merkle_root(coinbase_raw, merkle_branches)

    assert py_merkle_root == merkle_root   

def test_calculate_share_difficulty():
    coinbase_raw = EXAMPLE_BLOCK["extras"]["coinbaseRaw"]

    prevhash_chunks = [EXAMPLE_BLOCK["previousblockhash"][i : i + 8] for i in range(0, 64, 8)]
    prevhash = ""
    for chunk in prevhash_chunks:
        prevhash += th_proxy.protocol.difficulty.swap_endianness(chunk, 4).hex()

    prevhash = th_proxy.protocol.difficulty.swap_endianness(prevhash, 32).hex()

    # random data
    merkle_branches = [EXAMPLE_BLOCK["merkle_root"], "73625c6d1193965e93986ec5c5cde22c92b7f62d2db9db718f2743ba4b6be7a1"]

    test_job = { 
        "prevhash": prevhash,
        "coinb1": coinbase_raw[:8],
        "coinb2": coinbase_raw[10:],
        "merkle_branches": merkle_branches,
        "version": EXAMPLE_BLOCK["version"].to_bytes(4, 'big').hex(),
        "nbits": EXAMPLE_BLOCK["bits"].to_bytes(4, 'big').hex(),
    }

    nonce = "46010000"
    ntime = EXAMPLE_BLOCK["timestamp"].to_bytes(4, 'big').hex()
    extranonce1 = EXAMPLE_BLOCK["extras"]["coinbaseRaw"][8]
    extranonce2 = EXAMPLE_BLOCK["extras"]["coinbaseRaw"][9]
    version = None

    diff, bh = th_o3.protocol.difficulty.calculate_share_difficulty(test_job, extranonce1, extranonce2, ntime, nonce, version)
    print("diff:", diff, "bh:", bh)
    
    py_diff_, bh_py = py_diff.calculate_share_difficulty(test_job, extranonce1, extranonce2, ntime, nonce, version)

    assert bh == bh_py
    assert diff == py_diff_

def test_get_prevhash_hex():
    prevhash = EXAMPLE_BLOCK["previousblockhash"]
    prevhash_bytes_py = th_proxy.protocol.difficulty.get_prevhash_hex(prevhash)
    prevhash_bytes = th_o3.protocol.difficulty.get_prevhash_hex(prevhash)
    print("prev:", prevhash_bytes_py.hex())

    assert prevhash_bytes_py.hex() == prevhash_bytes

def test_handle_version():
    version = hex(EXAMPLE_BLOCK["version"])[2:]
    job_version = "12345678"
    version_py = th_proxy.protocol.difficulty.handle_version(version, job_version)
    version_o3 = th_o3.protocol.difficulty.handle_version(version, job_version)

    assert version_py == version_o3

def test_get_coinbase():
    coinbase_raw = EXAMPLE_BLOCK["extras"]["coinbaseRaw"]
    extranonce1 = EXAMPLE_BLOCK["extras"]["coinbaseRaw"][8]
    extranonce2 = EXAMPLE_BLOCK["extras"]["coinbaseRaw"][9]
    coinb1 = coinbase_raw[:8]
    coinb2 = coinbase_raw[10:]
    coinbase_py = th_proxy.protocol.difficulty.get_coinbase(coinb1, extranonce1, extranonce2, coinb2)
    coinbase_o3 = th_o3.protocol.difficulty.get_coinbase(coinb1, extranonce1, extranonce2, coinb2)

    assert coinbase_py == coinbase_o3

def test_block_header_from_job():
    coinbase_raw = EXAMPLE_BLOCK["extras"]["coinbaseRaw"]

    prevhash_chunks = [EXAMPLE_BLOCK["previousblockhash"][i : i + 8] for i in range(0, 64, 8)]
    prevhash = ""
    for chunk in prevhash_chunks:
        prevhash += th_proxy.protocol.difficulty.swap_endianness(chunk, 4).hex()

    prevhash = th_proxy.protocol.difficulty.swap_endianness(prevhash, 32).hex()

    test_job = { 
        "prevhash": prevhash,
        "coinb1": coinbase_raw[:8],
        "coinb2": coinbase_raw[10:],
        "merkle_branches": [
            EXAMPLE_BLOCK["merkle_root"],
            f"{random.randint(0, 2**32 - 1):064x}"
        ],
        "version": EXAMPLE_BLOCK["version"].to_bytes(4, 'big').hex(),
        "nbits": EXAMPLE_BLOCK["bits"].to_bytes(4, 'big').hex(),
    }

    nonce = "46010000"
    ntime = EXAMPLE_BLOCK["timestamp"].to_bytes(4, 'big').hex()
    extranonce1 = EXAMPLE_BLOCK["extras"]["coinbaseRaw"][8]
    extranonce2 = EXAMPLE_BLOCK["extras"]["coinbaseRaw"][9]
    version = None

    block_header_py = th_proxy.protocol.difficulty.block_header_from_job(test_job, extranonce1, extranonce2, ntime, nonce, version)
    block_header = th_o3.protocol.difficulty.block_header_from_job(test_job, extranonce1, extranonce2, ntime, nonce, version)

    assert block_header_py.hex() == block_header.hex()

    assert block_header.hex()[-12:-8] == EXAMPLE_BLOCK["extras"]["header"][-12:-8]
    assert block_header.hex()[:36] == EXAMPLE_BLOCK["extras"]["header"][:36]

