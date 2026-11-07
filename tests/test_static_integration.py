from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from api.state import AppState


def test_dynamics_assets_are_served_before_root_fallback(tmp_path):
    root = Path(__file__).resolve().parents[1]
    state = AppState(db_path=tmp_path/'run.sqlite', autoload=False, calibrate_audio=False, web_dir=root/'web')
    with TestClient(create_app(state)) as client:
        assert client.get('/dynamics/embed-demo.html').status_code == 200
        response = client.get('/dynamics/bootstrap.js')
        assert response.status_code == 200
        assert 'javascript' in response.headers['content-type']
        assert client.post('/dynamics/bootstrap.js').status_code == 405
        assert client.get('/dynamics/.env').status_code == 404
        assert client.get('/api/not-a-method').status_code == 404
