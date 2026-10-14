"""What a teammate's answer never shows (redact.py): each number and secret known by
its form is hidden in place, and what people work with goes out as written."""

from __future__ import annotations

import pytest

import redact


@pytest.mark.parametrize(
    ("text", "shown", "kind"),
    [
        (
            "등본에 870314-1234567로 적혀 있었다.",
            "등본에 [주민등록번호 가림]로 적혀 있었다.",
            "resident",
        ),
        ("번호:8703141234567 입니다", "번호:[주민등록번호 가림] 입니다", "resident"),
        ("050101 4123456", "[주민등록번호 가림]", "resident"),
        (
            "비자 서류에 850315-5123456 적었다.",
            "비자 서류에 [외국인등록번호 가림] 적었다.",
            "foreign",
        ),
        (
            "항공권은 M123A4567로 끊었다",
            "항공권은 [여권번호 가림]로 끊었다",
            "passport",
        ),
        ("여권번호 M12345678", "여권번호 [여권번호 가림]", "passport"),
        (
            "렌터카는 면허 11-23-456789-01로",
            "렌터카는 면허 [운전면허번호 가림]로",
            "driver",
        ),
        (
            "결제는 4111 1111 1111 1111로 했다.",
            "결제는 [카드번호 가림]로 했다.",
            "card",
        ),
        ("9410-1234-5678-9010", "[카드번호 가림]", "card"),
        ("378282246310005", "[카드번호 가림]", "card"),
        (
            "국민은행 123456-78-901234 김민수",
            "국민은행 [계좌번호 가림] 김민수",
            "account",
        ),
        ("정산은 계좌 1002345678901로", "정산은 계좌 [계좌번호 가림]로", "account"),
        ("지금 37.5563, 126.9236", "지금 [좌표 가림]", "coordinate"),
        (
            "테스트할 땐 sk-proj-9fK2mQx7Lp3vT8wZ1aB4cD6eF0gH 써.",
            "테스트할 땐 [API 키 가림] 써.",
            "api_key",
        ),
        ("ghp_" + "a1B2" * 9, "[API 키 가림]", "api_key"),
        ("AKIAIOSFODNN7EXAMPLE", "[API 키 가림]", "api_key"),
        (
            "Authorization: Bearer eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4",
            "Authorization: Bearer [API 키 가림]",
            "api_key",
        ),
        (
            "키는\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----\n끝",
            "키는\n[비밀 키 가림]\n끝",
            "private_key",
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nMIIEvQ…[2000 more characters]",
            "[비밀 키 가림]",
            "private_key",
        ),
        (
            "postgres://app:s3cret@db:5432/x",
            "postgres://app:[비밀번호 가림]@db:5432/x",
            "password",
        ),
        (
            "서버 접속 비번은 Qwer!2345.",
            "서버 접속 비번은 [비밀번호 가림].",
            "password",
        ),
        ("password=hunter2", "password=[비밀번호 가림]", "password"),
        ('"token": "abc123def456"', '"token": "[비밀번호 가림]"', "password"),
    ],
)
def test_each_kind_is_hidden_in_place(text: str, shown: str, kind: str) -> None:
    hidden, counts = redact.redact(text)
    assert hidden == shown
    assert counts == {kind: 1}


@pytest.mark.parametrize(
    "text",
    [
        # What people work with, which the Jev gate judges by what is said around it.
        "김민수 휴대폰 010-2345-6789",
        "결과는 minsu.kim87@gmail.com으로",
        "주차 등록은 12가 3456으로 했다.",
        "고객센터 1588-1234, 본사 02-1234-5678",
        "부산 지사 051-123-4567",
        # Numbers of work.
        "2026-10-14 12:34:56, PR #1234, build 20261014, 커밋 3691610e",
        "1760432400000",  # a time in milliseconds: month 60
        "90010112345678",  # one digit too many
        "4111 1111 1111 1112",  # the check digit is wrong
        "F1 0.8734, 0.9123",  # scores, not a place
        "서버 211.234.10.5:8443, v0.8.1",
        "S20231014 빌드",  # a serial without 여권 before it
        "sk-learn 버전",
        # A password or token named with no secret after it.
        "비밀번호 변경 화면을 고쳤다",
        "token 수가 1500을 넘으면 잘린다",
        "token limit 4096",
        "password: ${DB_PASSWORD}",
        "client_secret is required",
        "cd $(pwd)/web",
    ],
)
def test_what_people_work_with_goes_out_as_written(text: str) -> None:
    assert redact.redact(text) == (text, {})


def test_every_piece_of_text_in_a_result_is_hidden_and_the_rest_kept() -> None:
    result = {
        "messages": [
            {"content": "배포는 금요일", "id": 7, "at": None},
            {"content": "카드 4111 1111 1111 1111, 주민 870314-1234567"},
        ],
        "conversation_ids": ("s-1", "s-2"),
        "total": 2,
    }

    hidden, counts = redact.redact(result)

    assert hidden == {
        "messages": [
            {"content": "배포는 금요일", "id": 7, "at": None},
            {"content": "카드 [카드번호 가림], 주민 [주민등록번호 가림]"},
        ],
        "conversation_ids": ["s-1", "s-2"],
        "total": 2,
    }
    assert counts == {"card": 1, "resident": 1}
    assert result["messages"][1]["content"].startswith("카드 4111"), (
        "the result is copied"
    )


def test_a_question_holding_one_persons_number_is_named() -> None:
    assert (
        redact.identifier("870314-1234567 누구 거야?") == "resident registration number"
    )
    assert redact.identifier("여권번호 M12345678 찾아줘") == "passport number"
    assert redact.identifier("4111-1111-1111-1111") == "card number"
    assert redact.identifier("password=hunter2") is None, (
        "a secret is hidden, not refused"
    )
    assert redact.identifier("로그인 화면 어디까지 했어?") is None


def test_jev_reads_every_mark_the_same() -> None:
    hidden, _ = redact.redact(
        {"messages": [{"content": "여권 M123A4567, 카드 4111 1111 1111 1111"}]}
    )
    assert redact.unnamed(hidden) == {
        "messages": [{"content": "여권 [가림], 카드 [가림]"}]
    }
    assert redact.unnamed("[참고 가림] 그대로") == "[참고 가림] 그대로", (
        "only the marks redact.py writes"
    )
