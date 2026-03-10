import apps.driver_app.driver_agent_indie as legacy_driver_agent_module
import apps.ride_hail.driver.agent_impl as canonical_driver_agent_impl


def test_driver_agent_hs_shim_points_to_canonical_haversine_module():
    assert legacy_driver_agent_module.hs is canonical_driver_agent_impl.hs


def test_driver_agent_hs_monkeypatch_target_hits_canonical_module(monkeypatch):
    marker = object()
    monkeypatch.setattr(legacy_driver_agent_module.hs, "haversine", marker)
    assert canonical_driver_agent_impl.hs.haversine is marker
