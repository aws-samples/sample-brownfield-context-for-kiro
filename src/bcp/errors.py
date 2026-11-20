"""Error types with the CLI exit codes they map to."""


class BcpError(Exception):
    """Base class for expected, user-facing errors."""

    exit_code: int = 1


class BcpInputError(BcpError):
    """Bad input: missing file, invalid JSON, bad config value, unsafe path."""

    exit_code = 2


class ReviewRecordError(BcpError):
    """Invalid review record or sign-off file."""

    exit_code = 3


class OutputWriteError(BcpError):
    """A write, replace or delete under the target failed or would clobber a user file."""

    exit_code = 4
