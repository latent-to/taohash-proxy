"""
Authentication module for both token-based and signature-based verification.
"""

import hashlib
import os
import time
from pathlib import Path
from typing import Dict, Optional

import toml
from bittensor_wallet import Keypair
from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from ..utils.logger import get_logger

logger = get_logger(__name__)

# Token-based authentication - valis
security = HTTPBearer()

# Signature-based authentication - holders
X_PUBKEY = APIKeyHeader(name="X-PubKey", auto_error=False)
X_TIMESTAMP = APIKeyHeader(name="X-Timestamp", auto_error=False)
X_SIGNATURE = APIKeyHeader(name="X-Signature", auto_error=False)

API_TOKENS = set(
    token.strip()
    for token in os.environ.get("API_TOKENS", "").split(",")
    if token.strip()
)

REWARDS_POST_TOKEN = os.environ.get("REWARDS_POST_TOKEN", "")

AUTHORIZED_HOTKEYS = set(
    hotkey.strip()
    for hotkey in os.environ.get("AUTHORIZED_HOTKEYS", "").split(",")
    if hotkey.strip()
)

SIGNATURE_TIMESTAMP_WINDOW = int(os.environ.get("SIGNATURE_TIMESTAMP_WINDOW", "300"))

VALIDATOR_ACCESS_FILE_ENV = "VALIDATOR_ACCESS_FILE"
DEFAULT_VALIDATOR_ACCESS_FILE = (
    Path(__file__).resolve().parents[2] / "config" / "validator_access.toml"
)


def _determine_validator_access_path() -> Path:
    """Return configured validator access path or default location."""
    custom_path = os.environ.get(VALIDATOR_ACCESS_FILE_ENV)
    if custom_path:
        return Path(custom_path).expanduser()
    return DEFAULT_VALIDATOR_ACCESS_FILE


def _load_validator_access_tokens(path: Path, explicit: bool) -> Dict[str, str]:
    """
    Build a mapping of API tokens to validator aliases from the access config.

    Returns:
        Dict[str, str]: token -> alias
    """
    mapping: Dict[str, str] = {}
    if not path.exists():
        log_fn = logger.warning if explicit else logger.debug
        log_fn(f"Validator access config not found at {path}")
        return mapping

    try:
        config_data = toml.load(path)
    except Exception as exc:
        logger.error(f"Failed to load validator access config {path}: {exc}")
        return mapping

    validators = config_data.get("validators", config_data)
    if not isinstance(validators, dict):
        logger.warning(f"Validator access file {path} must contain a [validators] table")
        return mapping

    for alias, env_var_name in validators.items():
        if not isinstance(alias, str):
            continue
        if not isinstance(env_var_name, str):
            logger.warning(
                f"Validator '{alias}' entry must be a string env var name, got {type(env_var_name)}"
            )
            continue

        env_var = env_var_name.strip()
        if not env_var:
            logger.warning(f"Validator '{alias}' env var name is empty")
            continue

        token_value = os.environ.get(env_var, "").strip()
        if not token_value:
            logger.warning(
                f"Validator '{alias}' token env var '{env_var}' is not set or empty"
            )
            continue

        mapping[token_value] = alias

    if mapping:
        logger.info(
            "Validator access config loaded for: %s", ", ".join(sorted(mapping.values()))
        )

    return mapping


VALIDATOR_ACCESS_PATH = _determine_validator_access_path()
API_TOKEN_ALIASES = _load_validator_access_tokens(
    VALIDATOR_ACCESS_PATH, explicit=os.environ.get(VALIDATOR_ACCESS_FILE_ENV) is not None
)
API_TOKENS.update(API_TOKEN_ALIASES.keys())


class SignatureAuth:
    """Signature-based authentication for holders."""

    def __init__(self):
        """Initialize signature auth."""
        self.timestamp_window = SIGNATURE_TIMESTAMP_WINDOW
        self.authorized_hotkeys = AUTHORIZED_HOTKEYS

        if self.authorized_hotkeys:
            for hotkey in self.authorized_hotkeys:
                logger.info(f"Authorized holder hotkey: {hotkey}")
        else:
            logger.warning(
                "Warning: No authorized hotkeys configured. Holder API will be publicly accessible."
            )

    async def verify_signature(
        self,
        request: Request,
        x_pubkey: Optional[str] = Security(X_PUBKEY),
        x_timestamp: Optional[str] = Security(X_TIMESTAMP),
        x_signature: Optional[str] = Security(X_SIGNATURE),
    ) -> str:
        """
        Verify the signature of a request.

        Args:
            request: The FastAPI request object
            x_pubkey: The public key of the holder
            x_timestamp: The timestamp of the request
            x_signature: The signature of the request

        Returns:
            The verified public key

        Raises:
            HTTPException: If the signature is invalid or the holder is not authorized
        """
        if not all([x_pubkey, x_timestamp, x_signature]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authentication headers",
            )

        # Check if holder is authorized
        if x_pubkey not in self.authorized_hotkeys and len(self.authorized_hotkeys) > 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Holder not authorized: {x_pubkey}",
            )

        # Verify timestamp is within window
        try:
            ts = int(x_timestamp)
            now = int(time.time())
            if abs(now - ts) > self.timestamp_window:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Timestamp outside of allowed window",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid timestamp format",
            )

        canonical_string = await self._get_canonical_string(request, x_timestamp)

        if len(canonical_string) > 256:
            message_to_sign = hashlib.blake2b(
                canonical_string.encode("utf-8"), digest_size=32
            ).digest()
        else:
            message_to_sign = canonical_string.encode("utf-8")

        # Verify holder's signature
        try:
            keypair = Keypair(ss58_address=x_pubkey)
            try:
                signature_bytes = bytes.fromhex(x_signature)
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid signature format: {str(e)}",
                )

            is_valid = keypair.verify(message_to_sign, signature_bytes)

            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature"
                )

            return x_pubkey

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Signature verification failed: {str(e)}",
            )

    async def _get_canonical_string(self, request: Request, timestamp: str) -> str:
        """
        Get the canonical string to verify the signature.

        Format:
            HTTP_METHOD + "\n" +
            REQUEST_PATH + "\n" +
            TIMESTAMP + "\n" +
            REQUEST_BODY
        """
        method = request.method
        path = request.url.path

        body = ""

        return f"{method}\n{path}\n{timestamp}\n{body}"


signature_auth = SignatureAuth()


# Token-based authentication - validators
async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """Verify API token."""
    token = credentials.credentials.strip()
    if not API_TOKENS or token not in API_TOKENS:
        raise HTTPException(status_code=403, detail="Invalid API token")
    alias = API_TOKEN_ALIASES.get(token)
    setattr(request.state, "validator_alias", alias)
    return token

# Token based authentication - pool rewards
async def verify_rewards_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """Verify rewards POST token."""
    token = credentials.credentials
    if not REWARDS_POST_TOKEN or token != REWARDS_POST_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid rewards token")
    return token

# Signature-based authentication - holders
async def verify_signature(
    hotkey: str = Security(signature_auth.verify_signature),
) -> str:
    """
    Authenticate the current holder using signature verification.

    Returns:
        str: The validated holder hotkey if authentication succeeds

    Raises:
        HTTPException: If authentication fails
    """
    return hotkey
