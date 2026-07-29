import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_ensure_event_loop_creates_loop_in_worker_thread(strategy_module):
    def worker():
        with pytest.raises(RuntimeError):
            asyncio.get_event_loop_policy().get_event_loop()

        loop = strategy_module.ensure_event_loop()
        current = asyncio.get_event_loop_policy().get_event_loop()
        return loop, current

    with ThreadPoolExecutor(max_workers=1) as executor:
        loop, current = executor.submit(worker).result()

    assert loop is current
    assert not loop.is_closed()


def test_connect_ib_prepares_event_loop_before_connect(strategy_module, monkeypatch):
    observed = {}

    def fake_ibkr_connect(host, port, client_id, **kwargs):
        observed["args"] = (host, port, client_id, kwargs)
        return object()

    monkeypatch.setattr(strategy_module, "ibkr_connect_ib", fake_ibkr_connect)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(strategy_module.connect_ib).result()

    assert observed["args"] == ("127.0.0.1", 4001, 1, {"timeout": 60})


def test_connect_ib_retries_with_offset_client_ids(strategy_module_factory, monkeypatch):
    module = strategy_module_factory(
        IBKR_CONNECT_ATTEMPTS="3",
        IBKR_CONNECT_RETRY_DELAY_SECONDS="0",
        IBKR_CLIENT_ID_RETRY_OFFSET="100",
    )
    observed = {"client_ids": []}

    def fake_ibkr_connect(host, port, client_id, **kwargs):
        observed["client_ids"].append(client_id)
        if len(observed["client_ids"]) < 3:
            raise TimeoutError("handshake timeout")
        return object()

    monkeypatch.setattr(module, "ibkr_connect_ib", fake_ibkr_connect)

    module.connect_ib()

    assert observed["client_ids"] == [1, 101, 201]


def test_connect_ib_rejects_quantconnect_execution_backend(strategy_module_factory, monkeypatch):
    module = strategy_module_factory(
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"default":{"execution_backend":"quantconnect",'
            '"account_ids":["U00000000"],"service_name":"interactive-brokers-qc-service",'
            '"quantconnect_project_id":12345678,"quantconnect_node_id":"LN-placeholder",'
            '"quantconnect_compile_id":"compile-placeholder"}}}'
        )
    )
    monkeypatch.setattr(
        module,
        "ibkr_connect_ib",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not connect gateway")),
    )

    with pytest.raises(RuntimeError, match="execution_backend='quantconnect'"):
        module.connect_ib()


def test_ib_connect_attempts_falls_back_when_invalid(strategy_module_factory):
    module = strategy_module_factory(IBKR_CONNECT_ATTEMPTS="0")

    assert module.IB_CONNECT_ATTEMPTS == 3


def test_ib_connect_timeout_can_be_overridden(strategy_module_factory):
    module = strategy_module_factory(IBKR_CONNECT_TIMEOUT_SECONDS="75")

    assert module.IB_CONNECT_TIMEOUT_SECONDS == 75


def test_ib_connect_timeout_falls_back_when_invalid(strategy_module_factory):
    module = strategy_module_factory(IBKR_CONNECT_TIMEOUT_SECONDS="bad")

    assert module.IB_CONNECT_TIMEOUT_SECONDS == 60


def test_instance_name_alias_is_used_as_host(strategy_module):
    assert strategy_module.IB_HOST is None
    assert strategy_module.get_ib_host() == "127.0.0.1"
    assert strategy_module.IB_HOST == "127.0.0.1"


def test_get_ib_host_resolves_lazily(strategy_module_factory, monkeypatch):
    module = strategy_module_factory(
        IB_GATEWAY_ZONE="us-central1-a",
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"default":{"ib_gateway_instance_name":"ib-gateway",'
            '"ib_gateway_mode":"live","ib_client_id":1}}}'
        ),
    )

    assert module.IB_HOST is None

    class FakeComputeDiscovery:
        def resolve_instance_ip(self, instance, zone, *, project_id, prefer_internal):
            assert project_id == "test-project"
            assert zone == "us-central1-a"
            assert instance == "ib-gateway"
            assert prefer_internal is True
            return "10.0.0.8"

    monkeypatch.setattr(module, "get_compute_discovery", FakeComputeDiscovery)
    monkeypatch.setattr(module, "get_project_id", lambda: "test-project")

    assert module.get_ib_host() == "10.0.0.8"
    assert module.IB_HOST == "10.0.0.8"


def test_get_ib_host_refreshes_zoned_gateway_ip(strategy_module_factory, monkeypatch):
    module = strategy_module_factory(
        IB_GATEWAY_ZONE="us-central1-a",
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"default":{"ib_gateway_instance_name":"ib-gateway",'
            '"ib_gateway_mode":"live","ib_client_id":1}}}'
        ),
    )
    resolved = iter(("10.0.0.8", "10.0.0.9"))
    monkeypatch.setattr(module, "resolve_gce_instance_ip", lambda *_args: next(resolved))

    assert module.get_ib_host() == "10.0.0.8"
    assert module.get_ib_host() == "10.0.0.8"
    assert module.refresh_ib_host() == "10.0.0.9"
    assert module.IB_HOST == "10.0.0.9"


def test_refresh_ib_host_keeps_cached_ip_when_gce_lookup_fails(strategy_module_factory, monkeypatch):
    module = strategy_module_factory(
        IB_GATEWAY_ZONE="us-central1-a",
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"default":{"ib_gateway_instance_name":"ib-gateway",'
            '"ib_gateway_mode":"live","ib_client_id":1}}}'
        ),
    )
    module.IB_HOST = "10.0.0.8"
    monkeypatch.setattr(
        module,
        "resolve_gce_instance_ip",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("compute API unavailable")),
    )

    assert module.refresh_ib_host() == "10.0.0.8"
    assert module.IB_HOST == "10.0.0.8"


def test_ib_gateway_mode_derives_paper_port(strategy_module_factory):
    module = strategy_module_factory(
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"default":{"ib_gateway_instance_name":"127.0.0.1",'
            '"ib_gateway_mode":"paper","ib_client_id":1}}}'
        )
    )

    assert module.IB_PORT == 4002


def test_account_group_can_override_ib_gateway_port(strategy_module_factory):
    module = strategy_module_factory(
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"default":{"ib_gateway_instance_name":"127.0.0.1",'
            '"ib_gateway_mode":"live","ib_gateway_port":4011,"ib_client_id":1}}}'
        )
    )

    assert module.IB_PORT == 4011


def test_ib_gateway_mode_is_required(strategy_module_factory):
    with pytest.raises(EnvironmentError, match="requires ib_gateway_mode"):
        strategy_module_factory(
            IB_ACCOUNT_GROUP_CONFIG_JSON=(
                '{"groups":{"default":{"ib_gateway_instance_name":"127.0.0.1",'
                '"ib_client_id":1}}}'
            )
        )


def test_resolve_gce_instance_ip_prefers_internal_by_default(strategy_module, monkeypatch):
    class FakeComputeDiscovery:
        def resolve_instance_ip(self, instance, zone, *, project_id, prefer_internal):
            assert project_id == "test-project"
            assert zone == "us-central1-a"
            assert instance == "ib-gateway"
            assert prefer_internal is True
            return "10.0.0.8"

    monkeypatch.setattr(strategy_module, "get_compute_discovery", FakeComputeDiscovery)
    monkeypatch.setattr(strategy_module, "get_project_id", lambda: "test-project")
    monkeypatch.delenv("IB_GATEWAY_IP_MODE", raising=False)

    resolved = strategy_module.resolve_gce_instance_ip("ib-gateway", "us-central1-a")

    assert resolved == "10.0.0.8"


def test_resolve_gce_instance_ip_can_use_external_mode(strategy_module, monkeypatch):
    class FakeComputeDiscovery:
        def resolve_instance_ip(self, instance, zone, *, project_id, prefer_internal):
            assert project_id == "test-project"
            assert zone == "us-central1-a"
            assert instance == "ib-gateway"
            assert prefer_internal is False
            return "35.211.181.174"

    monkeypatch.setattr(strategy_module, "get_compute_discovery", FakeComputeDiscovery)
    monkeypatch.setattr(strategy_module, "get_project_id", lambda: "test-project")
    monkeypatch.setenv("IB_GATEWAY_IP_MODE", "external")

    resolved = strategy_module.resolve_gce_instance_ip("ib-gateway", "us-central1-a")

    assert resolved == "35.211.181.174"


def test_default_ranking_pool_uses_voo_xlk_smh(strategy_module):
    assert "VOO" in strategy_module.RANKING_POOL
    assert "XLK" in strategy_module.RANKING_POOL
    assert "SMH" in strategy_module.RANKING_POOL
    assert "QQQ" not in strategy_module.RANKING_POOL


def test_group_config_ip_mode_is_used_when_env_not_set(strategy_module_factory):
    module = strategy_module_factory(
        IB_GATEWAY_IP_MODE=None,
        IB_ACCOUNT_GROUP_CONFIG_JSON=(
            '{"groups":{"default":{"ib_gateway_instance_name":"127.0.0.1",'
            '"ib_gateway_mode":"live","ib_gateway_ip_mode":"external","ib_client_id":1}}}'
        ),
    )

    assert module.get_ib_gateway_ip_mode() == "external"
