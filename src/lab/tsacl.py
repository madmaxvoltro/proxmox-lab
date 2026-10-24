"""Tailscale policy snippet that `lab init` prints for the admin to paste into the ACL editor.

lab never edits the policy itself."""

import json


def acl_snippet(admin_login: str, owners: dict, base_tag: str = "tag:server", prefix: str = "tag:user-") -> str:
    """`owners` maps lab username -> tailnet login."""
    tag_owners = {base_tag: ["autogroup:admin"]}
    ssh = [{
        "action": "accept", "src": ["autogroup:admin"], "dst": [base_tag],
        "users": ["autogroup:nonroot", "root"],
    }]
    for name, login in sorted(owners.items()):
        tag = prefix + name
        tag_owners[tag] = ["autogroup:admin"]
        ssh.append({"action": "accept", "src": [login], "dst": [tag], "users": [name]})
    body = {"tagOwners": tag_owners, "ssh": ssh}
    return (
        "// Merge into your tailnet policy (admin console > Access controls).\n"
        "// Existing `server` / `userapp` rules are untouched; adapt if yours already cover this.\n"
        + json.dumps(body, indent=2)
        + f"\n// The OAuth client for lab needs the 'auth_keys' write scope, restricted to {base_tag} and {prefix}*.\n"
    )
