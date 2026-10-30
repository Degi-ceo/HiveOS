"""The portable test command must survive Windows shell metacharacters."""

from tests.portable_process import _windows_python_shell_command


def test_windows_python_command_quotes_metacharacters():
    command = _windows_python_shell_command(r"C:\a&b\python.exe", "print('a&b')")
    assert command == '"C:\\a&b\\python.exe" -c "print(\'a&b\')"'
