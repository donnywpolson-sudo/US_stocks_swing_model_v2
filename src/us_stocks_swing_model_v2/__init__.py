"""Core contracts for the isolated US stock/ETF swing research system."""

from .errors import ContractError, IntegrityError, NetworkGuardError

__all__ = ["ContractError", "IntegrityError", "NetworkGuardError"]
__version__ = "0.1.0"

