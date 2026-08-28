class RpcError(RuntimeError):
    pass


class JupiterError(RuntimeError):
    pass


class DiscoveryError(RuntimeError):
    pass


class PumpLaunchError(RuntimeError):
    pass


class UnknownLaunchResultError(PumpLaunchError):
    """The provider may have accepted a real launch, so retrying is unsafe."""

    pass
