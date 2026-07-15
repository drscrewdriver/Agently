# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Network egress policy engine for sandbox backends.

Provides fine-grained network access control including cloud metadata API
blocking, domain allowlisting, and full network isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .protocol import SandboxConfig


# ---------------------------------------------------------------------------
# Rule types
# ---------------------------------------------------------------------------

@dataclass
class NetworkRule:
    """A single network policy rule."""

    action: str  # "allow" | "deny"
    dst: str
    reason: str
    port: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": self.action, "dst": self.dst, "reason": self.reason}
        if self.port is not None:
            d["port"] = self.port
        return d


@dataclass
class NetworkPolicy:
    """Evaluated network policy — a list of rules to apply."""

    rules: list[NetworkRule] = field(default_factory=list)

    @property
    def is_completely_blocked(self) -> bool:
        """Whether all network access is denied."""
        return any(r.action == "deny" and r.dst == "*" for r in self.rules)

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [r.to_dict() for r in self.rules]}


# ---------------------------------------------------------------------------
# NetworkPolicyEngine
# ---------------------------------------------------------------------------

class NetworkPolicyEngine:
    """
    Network egress policy engine.

    Evaluates a SandboxConfig and produces a NetworkPolicy with concrete rules
    for iptables / Docker network configuration.

    Built-in blocked endpoints (cloud metadata APIs):
    - 169.254.169.254 (AWS / GCP)
    - 100.100.100.200 (Alibaba Cloud)
    - metadata.google.internal (GCP)
    - metadata.azure.com (Azure)
    """

    BLOCKED_ENDPOINTS: dict[str, str] = {
        "169.254.169.254": "aws_gcp_metadata",
        "100.100.100.200": "alicloud_metadata",
        "metadata.google.internal": "gcp_metadata",
        "metadata.azure.com": "azure_metadata",
    }

    async def evaluate(self, config: SandboxConfig) -> NetworkPolicy:
        """
        Evaluate network policy from sandbox configuration.

        Three modes:
        1. network_enabled=False → deny all
        2. network_enabled=True + network_allowlist → deny all + allow listed
        3. network_enabled=True + no allowlist → allow all (except metadata)
        """
        rules: list[NetworkRule] = []

        # Always block cloud metadata when configured
        if config.block_cloud_metadata:
            for endpoint, reason in self.BLOCKED_ENDPOINTS.items():
                rules.append(NetworkRule(
                    action="deny",
                    dst=endpoint,
                    reason=reason,
                ))

        # Mode 1: Network completely disabled
        if not config.network_enabled:
            rules.append(NetworkRule(
                action="deny",
                dst="*",
                reason="network_disabled",
            ))
            return NetworkPolicy(rules=rules)

        # Mode 2: Allowlist mode
        if config.network_allowlist:
            # Deny all first, then allow specific destinations
            rules.append(NetworkRule(
                action="deny",
                dst="*",
                reason="allowlist_mode",
            ))
            for allowed in config.network_allowlist:
                rules.append(NetworkRule(
                    action="allow",
                    dst=allowed,
                    reason="allowlisted",
                ))
            return NetworkPolicy(rules=rules)

        # Mode 3: Network enabled, no allowlist — only metadata blocked
        return NetworkPolicy(rules=rules)

    async def apply_to_docker_args(
        self,
        docker_args: list[str],
        policy: NetworkPolicy,
    ) -> list[str]:
        """
        Apply network policy to Docker container creation arguments.

        Modifies the argument list to enforce network restrictions.
        """
        if policy.is_completely_blocked:
            # Remove any existing --network arg and set to none
            filtered = self._remove_network_args(docker_args)
            filtered.extend(["--network", "none"])
            return filtered

        # For allowlist or partial policies, use a custom network
        # (Full iptables management is out of scope for this implementation;
        #  the Docker --network=none provides the strongest isolation.)
        return docker_args

    @staticmethod
    def _remove_network_args(args: list[str]) -> list[str]:
        """Remove --network related arguments from a Docker args list."""
        result: list[str] = []
        skip_next = False
        for i, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if arg == "--network":
                skip_next = True
                continue
            if arg.startswith("--network="):
                continue
            result.append(arg)
        return result
