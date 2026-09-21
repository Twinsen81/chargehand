import pytest

from chargehand import __version__
from chargehand.cli import main


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_arguments_prints_help(capsys):
    assert main([]) == 0
    assert "usage: chargehand" in capsys.readouterr().out
