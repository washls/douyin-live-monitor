from unittest.mock import patch

from douyin_monitor.abogus import generate_abogus


def test_fixed_abogus_signature_vector():
    with patch("douyin_monitor.abogus.time.time", return_value=1700000000):
        signature = generate_abogus(
            "aid=6383&sec_user_id=MS4wLjABAAAA-test",
            "Mozilla/5.0 Test",
        )

    assert signature == "DFSzsd6I03ZOLZvQoSNf0zHMcBJE"
