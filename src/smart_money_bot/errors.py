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


def describe_exception(error: BaseException) -> str:
    """A log line that is still useful when the exception has no message.

    Production logged dozens of ``Fomo fresh analysis 55sWLQ39: `` lines with
    nothing after the colon.  They were ``asyncio.TimeoutError``, whose ``str()``
    is the empty string, so the one thing an operator needed — *that a 30-second
    analysis budget was being blown* — was the one thing the log did not say.
    """

    message = str(error).strip()
    name = type(error).__name__
    if message:
        return f"{name}: {message}" if name not in message else message
    if isinstance(error, TimeoutError):
        return "TimeoutError (the operation exceeded its time budget)"
    return f"{name} (no message)"
