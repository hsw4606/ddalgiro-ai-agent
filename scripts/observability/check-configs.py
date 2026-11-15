#!/usr/bin/env python3
"""운영 서버 없이 Prometheus·Alertmanager 설정과 경보의 발생·해제를 검증한다.

실행: PyYAML 이 있는 Python 으로 scripts/observability/check-configs.py
Docker 를 사용한다. 임시 폴더에서 렌더링하고, 메일 수신자·키·운영 클러스터에는 연결하지 않는다.
"""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ddalgiro-observe-") as directory:
        root = Path(directory)
        (root / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/render.sh", root / "scripts/render.sh")
        shutil.copytree(ROOT / "k8s", root / "k8s")
        shutil.copy(ROOT / "site.env.example", root / "site.env")
        subprocess.run(["bash", str(root / "scripts/render.sh")], check=True)
        objects = list(yaml.safe_load_all((root / "rendered/70-monitoring.yaml").read_text()))
        config = next(o["data"] for o in objects if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "prometheus-config")
        for name, content in config.items():
            (root / name).write_text(content)
        shutil.copy(ROOT / "tests/monitoring/alerts.test.yaml", root / "alerts.test.yaml")
        # 문법 검사만 하며 이 설정으로 서버를 실행하거나 이메일을 보내지 않는다.
        (root / "alertmanager.yaml").write_text(json.dumps({
            "global": {"smtp_smarthost": "localhost:1025", "smtp_from": "test@example.com", "smtp_require_tls": True},
            "route": {"receiver": "operations"},
            "receivers": [{"name": "operations", "email_configs": [{"to": "test@example.com"}]}],
        }))
        images = {o["metadata"]["name"]: o["spec"]["template"]["spec"]["containers"][0]["image"]
                  for o in objects if o["kind"] == "StatefulSet"}
        for command in (["check", "config", "/etc/prometheus/prometheus.yaml"],
                        ["test", "rules", "/etc/prometheus/alerts.test.yaml"]):
            subprocess.run(["docker", "run", "--rm", "--entrypoint", "/bin/promtool", "-v",
                            f"{root}:/etc/prometheus:ro", images["prometheus"], *command], check=True)
        subprocess.run(["docker", "run", "--rm", "--entrypoint", "/bin/amtool", "-v",
                        f"{root}:/etc/alertmanager:ro", images["alertmanager"],
                        "check-config", "/etc/alertmanager/alertmanager.yaml"], check=True)


if __name__ == "__main__":
    main()
