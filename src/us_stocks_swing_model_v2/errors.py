class ContractError(ValueError):
    """A declared scientific or data contract was violated."""


class IntegrityError(RuntimeError):
    """Content, lineage, or append-only integrity could not be proven."""


class LockHeldError(RuntimeError):
    """A one-writer lock is already held."""


class NetworkGuardError(PermissionError):
    """A network-capable operation was not explicitly authorized."""


class EvaluationAuthorizationError(PermissionError):
    """A real-data evaluation is absent from the immutable trial registry."""

