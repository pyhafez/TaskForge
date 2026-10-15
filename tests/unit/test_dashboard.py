from django.contrib.staticfiles import finders
from django.test import Client, override_settings


@override_settings(DEBUG=True)
def test_dashboard_shell_and_assets_are_available():
    response = Client().get("/")
    assert response.status_code == 200
    assert b"TaskForge" in response.content
    assert b"dashboard.js" in response.content
    assert "script-src 'self'" in response["Content-Security-Policy"]
    assert finders.find("taskforge/dashboard.css")
    assert finders.find("taskforge/dashboard.js")
