"""사용자 스크립트 예시. run(case)에서 assert로 검증하면 실패 시 자동으로 Fail + 이슈 등록된다.
YAML로 표현하기 어려운 흐름(반복, 조건, 여러 API 연계 등)을 여기에 자유롭게 작성하면 된다."""
import requests


def run(case):
    users = requests.get("https://jsonplaceholder.typicode.com/users", timeout=10).json()
    assert len(users) == 10, f"사용자 수: 기대 10, 실제 {len(users)}"
    bad = [u["id"] for u in users if "@" not in u["email"]]
    assert not bad, f"이메일 형식 오류 사용자 id: {bad}"
