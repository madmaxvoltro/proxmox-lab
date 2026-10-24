"""Interactive prompts, behind an interface so init is testable and scriptable."""

from .errors import UsageError


class Prompter:
    def choose(self, label: str, options: list, default=None):
        """options: [(value, description)]. Returns the chosen value."""
        raise NotImplementedError

    def confirm(self, message: str, default: bool = False) -> bool:
        raise NotImplementedError

    def ask(self, label: str, default: str | None = None) -> str:
        raise NotImplementedError

    def secret(self, label: str) -> str:
        """Like ask(), but nothing is echoed to the screen."""
        raise NotImplementedError

    def info(self, message: str) -> None:
        print(message)


class TerminalPrompter(Prompter):
    def secret(self, label):
        import getpass
        return getpass.getpass(f"{label} (hidden): ").strip()

    def choose(self, label, options, default=None):
        print(f"\n{label}")
        for i, (value, desc) in enumerate(options, 1):
            print(f"  {i}) {value}" + (f"  - {desc}" if desc else "") + ("  [default]" if value == default else ""))
        while True:
            raw = input("> ").strip()
            if not raw and default is not None:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            print("enter a number from the list")

    def confirm(self, message, default=False):
        raw = input(f"{message} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not raw else raw in ("y", "yes")

    def ask(self, label, default=None):
        raw = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
        return raw or (default or "")


class ScriptedPrompter(Prompter):
    """Non-interactive: answers come from a dict keyed by label prefix. Unanswered choices
    fall back to the default; unanswered confirms are refused unless assume_yes."""

    def __init__(self, answers: dict | None = None, assume_yes: bool = False):
        self.answers = answers or {}
        self.assume_yes = assume_yes
        self.log: list = []

    def _lookup(self, label):
        for prefix, value in self.answers.items():
            if label.startswith(prefix):
                return True, value
        return False, None

    def choose(self, label, options, default=None):
        found, value = self._lookup(label)
        value = value if found else default
        if value is None or value not in [o[0] for o in options]:
            raise UsageError(f"no valid answer for: {label} (options: {', '.join(str(o[0]) for o in options)})")
        return value

    def confirm(self, message, default=False):
        found, value = self._lookup(message)
        return bool(value) if found else (self.assume_yes or default)

    def ask(self, label, default=None):
        found, value = self._lookup(label)
        value = value if found else default
        if value is None:
            raise UsageError(f"no answer for: {label}")
        return str(value)

    def secret(self, label):
        return self.ask(label)

    def info(self, message):
        self.log.append(message)
