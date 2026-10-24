"""Who is calling `lab`: the Tailscale identity of the SSH connection (SPEC section 17)."""

import json

from .runner import Runner


def caller_login(env: dict, runner: Runner) -> str | None:
    """Tailnet login name of the connecting client, or None if it cannot be determined.

    SSH_CONNECTION is "<client ip> <client port> <server ip> <server port>"; the host asks
    its local tailscaled who owns the client address."""
    conn = env.get("SSH_CONNECTION", "").split()
    if not conn:
        return None
    res = runner.run(["tailscale", "whois", "--json", conn[0]], check=False)
    if not res.ok:
        return None
    try:
        return json.loads(res.stdout)["UserProfile"]["LoginName"] or None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
