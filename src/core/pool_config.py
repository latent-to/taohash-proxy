"""
Pool configuration dataclass for the mining proxy.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PoolConfig:
    """
    Attributes:
        host: Pool hostname
        port: Pool port number
        user: Pool authentication username
        password: Pool authentication password
        label: Pool identifier label (e.g., 'normal', 'high_diff', or order_id)
    """

    host: str
    port: int
    user: str
    password: str
    label: str

    def to_dict(self) -> dict:
        """
        Convert to dict for serialization.

        Uses 'pass' key for compatibility with existing
        configs, Redis messages, and TOML files.
        """
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "pass": self.password,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PoolConfig":
        """
        Create PoolConfig from dict.

        Args:
            data: Dict with pool configuration. Must have 'host', 'port', 'user'.
                  Password can be 'pass' or 'password'. Label defaults to 'unknown' if not provided.

        Returns:
            PoolConfig instance

        Raises:
            KeyError: If required fields are missing
        """
        return cls(
            host=data["host"],
            port=int(data["port"]),
            user=data["user"],
            password=data.get("password") or data.get("pass", ""),
            label=data.get("label", "unknown"),
        )

    @property
    def connection_string(self) -> str:
        """Return host:port for logging."""
        return f"{self.host}:{self.port}"
