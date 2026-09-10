"""Phase 11.1 - RBAC + workspace isolation (JWT HS256, capability enums, audit log).

Team A gets repo-frontend only, team B gets backend+infra - enforced per request:
  role permissions  AND  tenant match  AND  resource (repo) allow-list.
Every decision (allow or deny) is journalled to .cortex_memory/audit.jsonl - SOC2-style,
append-only, no PII beyond user_id.

Mint tokens for local/dev use:
  python -m server.rbac mint --sub alice --roles developer --tenant team-a \
      --repos acme/frontend --secret dev-secret
"""
import argparse
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Set

import jwt


class Role(str, Enum):
    ADMIN = "admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"
    AGENT_OPERATOR = "agent_operator"


class Permission(str, Enum):
    READ_CODE = "code:read"
    WRITE_CODE = "code:write"
    EXECUTE_AGENT = "agent:execute"
    MANAGE_INFRA = "infra:manage"
    VIEW_COST = "cost:view"


ROLE_PERMISSIONS: Dict[Role, Set[Permission]] = {
    Role.ADMIN: {p for p in Permission},
    Role.DEVELOPER: {Permission.READ_CODE, Permission.WRITE_CODE, Permission.EXECUTE_AGENT, Permission.VIEW_COST},
    Role.VIEWER: {Permission.READ_CODE, Permission.VIEW_COST},
    Role.AGENT_OPERATOR: {Permission.EXECUTE_AGENT},
}


@dataclass
class Principal:
    user_id: str
    roles: List[Role]
    tenant_id: str
    allowed_repos: List[str] = field(default_factory=list)
    exp: int = 0

    def to_payload(self) -> Dict:
        return {"user_id": self.user_id, "roles": [r.value for r in self.roles],
                "tenant_id": self.tenant_id, "allowed_repos": self.allowed_repos, "exp": self.exp}


class AuthZ:
    def __init__(self, secret: str, leeway: int = 30):
        self.secret = secret
        self.leeway = leeway

    def mint(self, p: Principal, ttl_s: int = 3600) -> str:
        p.exp = int(time.time()) + ttl_s
        return jwt.encode(p.to_payload(), self.secret, algorithm="HS256")

    def decode_token(self, token: str) -> Principal:
        payload = jwt.decode(token, self.secret, algorithms=["HS256"], leeway=self.leeway)
        return Principal(user_id=payload["user_id"], roles=[Role(r) for r in payload.get("roles", [])],
                         tenant_id=payload["tenant_id"], allowed_repos=payload.get("allowed_repos", []),
                         exp=payload.get("exp", 0))

    def check(self, principal: Principal, perm: Permission, resource: str = "", tenant: str = "") -> bool:
        if tenant and principal.tenant_id != tenant:          # hard workspace isolation
            return False
        has = any(perm in ROLE_PERMISSIONS.get(r, set()) for r in principal.roles)
        if not has:
            return False
        if perm in (Permission.READ_CODE, Permission.WRITE_CODE, Permission.EXECUTE_AGENT, Permission.MANAGE_INFRA):
            if resource and resource not in principal.allowed_repos and Role.ADMIN not in principal.roles:
                return False
        return True


AUDIT = Path(__file__).resolve().parents[1] / ".cortex_memory" / "audit.jsonl"


def audit(principal: Principal | None, perm: str, resource: str, tenant: str, decision: str, reason: str = "") -> None:
    rec = {"ts": round(time.time(), 1), "user": getattr(principal, "user_id", "anonymous"),
           "tenant": tenant, "perm": perm, "resource": resource, "decision": decision, "reason": reason}
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["mint"])
    ap.add_argument("--sub", required=True)
    ap.add_argument("--roles", default="viewer")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--repos", default="")
    ap.add_argument("--secret", default="dev-secret")
    a = ap.parse_args()
    tok = AuthZ(a.secret).mint(Principal(a.sub, [Role(x.strip()) for x in a.roles.split(",")],
                                         a.tenant, [x for x in a.repos.split(",") if x]))
    print(tok)
