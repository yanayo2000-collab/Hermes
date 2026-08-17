from app.creative_image_generation import _currency_reward_review


def test_indonesian_dot_grouped_task_reward_is_parsed_as_thousands() -> None:
    review = _currency_reward_review(
        "Tugas selesai; Reward masuk ke akun TUGAO; Rp 3.000",
        "ID",
    )

    assert review["status"] == "passed"
    assert review["amounts"] == [
        {
            "raw": "Rp 3.000",
            "amount": 3000.0,
            "bucket": "task_reward",
            "status": "passed",
        }
    ]


def test_indonesian_comma_grouped_task_reward_is_parsed_as_thousands() -> None:
    review = _currency_reward_review("Tugas selesai Rp 3,000", "ID")

    assert review["status"] == "passed"
    assert review["amounts"][0]["amount"] == 3000.0


def test_indonesian_reward_below_minimum_remains_rejected() -> None:
    review = _currency_reward_review("Tugas selesai Rp 500", "ID")

    assert review["status"] == "auto_rejected"
    assert review["amounts"][0]["amount"] == 500.0
