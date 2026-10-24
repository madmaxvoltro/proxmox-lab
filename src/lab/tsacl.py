"""Tailscale policy snippet that `lab setup` / `lab init` print for the admin to paste into the ACL editor.

lab never edits the policy itself. Project VMs carry a dedicated tag (`tag:lab` by default) plus one tag
per owner. The dedicated tag is deliberate: it has no network grants of its own, so a project VM cannot
reach anything on the tailnet unless the policy says so, and it does not inherit the permissions of
your real servers (`tag:server`)."""

import json

DEFAULT_BASE_TAG = "tag:lab"


def acl_snippet(admin_login: str, owners: dict, base_tag: str = DEFAULT_BASE_TAG, prefix: str = "tag:user-") -> str:
    """`owners` maps lab username -> tailnet login."""
    tag_owners = {base_tag: ["autogroup:admin"]}
    grants = [{"src": ["autogroup:admin"], "dst": [base_tag], "ip": ["tcp:22"]}]
    ssh = [{
        "action": "accept", "src": ["autogroup:admin"], "dst": [base_tag],
        "users": ["autogroup:nonroot", "root"],
    }]
    for name, login in sorted(owners.items()):
        tag = prefix + name
        tag_owners[tag] = ["autogroup:admin"]
        grants.append({"src": [login], "dst": [tag], "ip": ["tcp:22"]})
        ssh.append({"action": "accept", "src": [login], "dst": [tag], "users": [name]})
    body = {"tagOwners": tag_owners, "grants": grants, "ssh": ssh}
    return (
        "// Merge into your tailnet policy (admin console > Access controls).\n"
        "// Add the entries to your existing tagOwners / grants / ssh sections; duplicate section names are invalid.\n"
        f"// {base_tag} gets no outbound grants: project VMs cannot reach your other devices unless you add a rule.\n"
        + json.dumps(body, indent=2)
        + f"\n// The OAuth client for lab needs the 'Auth Keys: write' scope, restricted to {base_tag} and {prefix}*.\n"
    )
