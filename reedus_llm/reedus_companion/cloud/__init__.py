"""cloud mode: gemini brain + inworld voice."""

class CloudUnavailable(RuntimeError):
    """A cloud provider can't service the request; the caller should fall back to local."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = True):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
