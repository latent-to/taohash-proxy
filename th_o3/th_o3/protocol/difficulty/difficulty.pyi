from typing import TypedDict, Optional

class Job(TypedDict):
    prevhash: str
    coinb1: str
    coinb2: str
    merkle_branches: list[str]
    version: str
    nbits: str

def get_block_header(version: str, prevhash: str, merkle_root: str, ntime: str, nbits: str, nonce: str) -> bytes:
    ...

def handle_version(version: str, job_version: str) -> str:
    ...

def get_difficulty(hash: str) -> float:
    ...

def calculate_share_difficulty(job: Job, extranonce1: str, extranonce2: str, ntime: str, nonce: str, version: Optional[str] = None) -> tuple[float, str]:
    ...

def get_merkle_root(coinbase: str, merkle_branches: list[str]) -> str:
    ...

def get_prevhash_hex(prevhash: str) -> str:
    ...
    
from typing import Any, Optional

def get_share_difficulty(
    job: dict[str, Any],
    extranonce1: str,
    extranonce2: str,
    ntime: str,
    nonce: str,
    version: Optional[str] = None,
) -> tuple[float, str]:
    ...

def find_nonce(
    job: dict[str, Any],
    extranonce1: str,
    extranonce2: str,
    ntime: str,
    version: Optional[str],
    start: int,
    end: int,
    threshold: float,
) -> tuple[bytes, str]:
    ...
