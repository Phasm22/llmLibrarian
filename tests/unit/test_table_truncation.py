import cli


def test_truncate_mid():
    assert cli.cmd_ls.__name__  # ensure module loaded
    # Access helpers via module globals
    trunc_mid = cli._truncate_mid  # type: ignore[attr-defined]
    text = "Become a Linear Algebra Master"
    out = trunc_mid(text, 12)
    assert out.startswith("B")
    assert "..." in out
    assert out.endswith("er")


def test_truncate_tail():
    trunc_tail = cli._truncate_tail  # type: ignore[attr-defined]
    text = "/home/user/Documents/Become a Linear Algebra Master"
    out = trunc_tail(text, 20)
    assert out.startswith("...")
    assert out.endswith("Master")
