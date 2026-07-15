# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for network policy engine."""

import asyncio

import pytest

from agently.builtins.sandbox.network_policy import (
    NetworkPolicy,
    NetworkPolicyEngine,
    NetworkRule,
)
from agently.builtins.sandbox.protocol import SandboxConfig


class TestNetworkRule:
    def test_deny_rule(self):
        rule = NetworkRule(action="deny", dst="169.254.169.254", reason="cloud_metadata")
        assert rule.action == "deny"
        assert rule.dst == "169.254.169.254"

    def test_to_dict(self):
        rule = NetworkRule(action="allow", dst="api.example.com", reason="allowlisted", port=443)
        d = rule.to_dict()
        assert d["action"] == "allow"
        assert d["port"] == 443


class TestNetworkPolicy:
    def test_completely_blocked(self):
        policy = NetworkPolicy(rules=[
            NetworkRule(action="deny", dst="*", reason="network_disabled"),
        ])
        assert policy.is_completely_blocked is True

    def test_not_completely_blocked(self):
        policy = NetworkPolicy(rules=[
            NetworkRule(action="deny", dst="169.254.169.254", reason="metadata"),
        ])
        assert policy.is_completely_blocked is False

    def test_empty_policy_not_blocked(self):
        policy = NetworkPolicy()
        assert policy.is_completely_blocked is False


class TestNetworkPolicyEngine:
    @pytest.fixture
    def engine(self):
        return NetworkPolicyEngine()

    def test_network_disabled_blocks_all(self, engine):
        config = SandboxConfig(network_enabled=False)
        policy = asyncio.run(engine.evaluate(config))
        assert policy.is_completely_blocked is True

    def test_network_disabled_blocks_metadata(self, engine):
        config = SandboxConfig(network_enabled=False, block_cloud_metadata=True)
        policy = asyncio.run(engine.evaluate(config))
        # Should have metadata blocks + deny-all
        deny_rules = [r for r in policy.rules if r.action == "deny"]
        assert len(deny_rules) >= 5  # 4 metadata + 1 deny-all

    def test_allowlist_mode(self, engine):
        config = SandboxConfig(
            network_enabled=True,
            network_allowlist=["api.example.com", "pypi.org"],
        )
        policy = asyncio.run(engine.evaluate(config))
        allow_rules = [r for r in policy.rules if r.action == "allow"]
        assert len(allow_rules) == 2
        assert any(r.dst == "api.example.com" for r in allow_rules)
        assert any(r.dst == "pypi.org" for r in allow_rules)
        # Should also have a deny-all rule
        deny_all = [r for r in policy.rules if r.action == "deny" and r.dst == "*"]
        assert len(deny_all) == 1
        # is_completely_blocked is True because deny-all exists (allow rules are exceptions)
        assert policy.is_completely_blocked is True

    def test_network_enabled_no_allowlist(self, engine):
        config = SandboxConfig(
            network_enabled=True,
            block_cloud_metadata=True,
        )
        policy = asyncio.run(engine.evaluate(config))
        # Only metadata blocks, no deny-all
        assert not any(r.dst == "*" for r in policy.rules)
        metadata_rules = [r for r in policy.rules if r.action == "deny"]
        assert len(metadata_rules) == 4  # AWS, GCP, Azure, Alicloud

    def test_metadata_blocking_disabled(self, engine):
        config = SandboxConfig(
            network_enabled=True,
            block_cloud_metadata=False,
        )
        policy = asyncio.run(engine.evaluate(config))
        assert len(policy.rules) == 0

    def test_blocked_endpoints_include_all_cloud_providers(self, engine):
        assert "169.254.169.254" in NetworkPolicyEngine.BLOCKED_ENDPOINTS
        assert "100.100.100.200" in NetworkPolicyEngine.BLOCKED_ENDPOINTS
        assert "metadata.google.internal" in NetworkPolicyEngine.BLOCKED_ENDPOINTS
        assert "metadata.azure.com" in NetworkPolicyEngine.BLOCKED_ENDPOINTS

    def test_remove_network_args(self):
        args = ["docker", "run", "--network", "bridge", "--memory", "512m"]
        result = NetworkPolicyEngine._remove_network_args(args)
        assert "--network" not in result
        assert "bridge" not in result
        assert "--memory" in result
        assert "512m" in result

    def test_remove_network_args_equals_form(self):
        args = ["docker", "run", "--network=host", "--memory", "512m"]
        result = NetworkPolicyEngine._remove_network_args(args)
        assert "--network=host" not in result
        assert "--memory" in result
