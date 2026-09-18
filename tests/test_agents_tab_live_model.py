import pytest
from fastapi.testclient import TestClient
from aether_engine.app import app, disabled_nodes, engine_state

client = TestClient(app)

def test_agents_nodes_list():
    disabled_nodes.clear()
    
    # By default, nodes are enabled
    response = client.get("/agents/nodes")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    for node in data:
        assert node["enabled"] is True
        
def test_agents_nodes_toggle():
    disabled_nodes.clear()
    
    # Disable "research"
    resp = client.patch("/agents/nodes/research/toggle", json={"enabled": False})
    assert resp.status_code == 200
    
    assert "research" in disabled_nodes
    
    # Check it reflects in list
    response = client.get("/agents/nodes")
    data = response.json()
    for node in data:
        if node["name"] == "research":
            assert node["enabled"] is False
        else:
            assert node["enabled"] is True
            
    # Re-enable
    resp = client.patch("/agents/nodes/research/toggle", json={"enabled": True})
    assert resp.status_code == 200
    assert "research" not in disabled_nodes
