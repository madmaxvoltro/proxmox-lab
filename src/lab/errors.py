"""Error types. Exit codes follow SPEC section 7."""

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_VERIFY = 4
EXIT_PARTIAL = 5


class LabError(Exception):
    exit_code = 1


class UsageError(LabError):
    exit_code = EXIT_USAGE


class PreconditionError(LabError):
    exit_code = EXIT_PRECONDITION


class VerificationError(LabError):
    exit_code = EXIT_VERIFY


class PartialError(LabError):
    exit_code = EXIT_PARTIAL


class CommandError(LabError):
    """A host command exited non-zero."""

    def __init__(self, argv, returncode, stderr=""):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(self.argv)} exited {returncode}: {stderr.strip()}")
