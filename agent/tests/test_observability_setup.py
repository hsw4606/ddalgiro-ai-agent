"""관측 배포 준비의 비밀값·기존 설정 호환성을 확인한다. 외부 서비스는 쓰지 않는다.

실행: python -m pytest tests/test_observability_setup.py -q
기존 키 파일 덮어쓰기, 명령 인자 유출, 추적 준비 전의 잘못된 상태 검사를 막는다.
"""
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(path):
    spec = importlib.util.spec_from_file_location("setup_test", ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_langfuse_env_is_private_and_never_overwritten(tmp_path):
    setup = module("scripts/langfuse/init-env.py")
    path = tmp_path / ".env"
    setup.initialize("langfuse.example.com", "operator@example.com", path)
    initial = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b"LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-" in initial
    with pytest.raises(FileExistsError):
        setup.initialize("langfuse.example.com", "operator@example.com", path)
    assert path.read_bytes() == initial


def test_alert_credentials_use_stdin_and_preserve_special_characters(monkeypatch):
    setup = module("scripts/observability/create-secrets.py")
    answers = iter(["smtp.example.com:587", "sender@example.com", "ops@example.com", "operator"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    password = 'SECRET-"PASSWORD\\VALUE'
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: password)
    config = setup.alert_config()
    assert config["global"]["smtp_auth_password"] == password
    assert config["global"]["smtp_require_tls"] is True
    calls = []
    monkeypatch.setattr(setup.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    setup.apply_secret("ai-observe", "alertmanager-config", {"alertmanager.yaml": json.dumps(config)})
    args, options = calls[0]
    assert password not in str(args)
    secret = json.loads(options["input"])
    assert json.loads(secret["stringData"]["alertmanager.yaml"])["global"]["smtp_auth_password"] == password


@pytest.mark.parametrize("mode", ["old-file", "enabled", "invalid-http"])
def test_render_supports_existing_config_and_prepared_langfuse(tmp_path, mode):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/render.sh", tmp_path / "scripts/render.sh")
    shutil.copytree(ROOT / "k8s", tmp_path / "k8s")
    text = (ROOT / "site.env.example").read_text()
    text = "\n".join(line for line in text.splitlines() if not line.startswith("LANGFUSE_"))
    if mode != "old-file":
        text += "\nLANGFUSE_ENABLED=true\nLANGFUSE_BASE_URL=" + ("http" if mode == "invalid-http" else "https") + "://langfuse.example.com\n"
    env = tmp_path / "site.env"
    env.write_text(text)
    p = subprocess.run(["bash", str(tmp_path / "scripts/render.sh"), str(env)], capture_output=True, text=True)
    if mode == "invalid-http":
        assert p.returncode != 0
        assert "HTTPS" in p.stderr
        return
    assert p.returncode == 0, p.stderr
    rendered = tmp_path / "rendered"
    objects = list(yaml.safe_load_all((rendered / "70-monitoring.yaml").read_text()))
    config = next(o["data"] for o in objects if o["metadata"]["name"] == "prometheus-config")
    jobs = yaml.safe_load(config["prometheus.yaml"])["scrape_configs"]
    langfuse = next(j for j in jobs if j["job_name"] == "langfuse")
    assert langfuse["static_configs"][0]["targets"] == ([] if mode == "old-file" else ["https://langfuse.example.com/api/public/health"])
