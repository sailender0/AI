"""Single-instance lock: the IPC-port bind must be exclusive — two acquirers
can never both hold it. Guards the Windows SO_REUSEADDR hole that let the
desktop agent launch twice (duplicate windows/tray icons).
"""
import agent.__main__ as entry

entry._IPC_PORT = 47999


def test_second_bind_fails_while_first_holds():
    first = entry._bind_instance_lock()
    assert first is not None
    try:
        assert entry._bind_instance_lock() is None
    finally:
        first.close()


def test_lock_is_reacquirable_after_release():
    first = entry._bind_instance_lock()
    assert first is not None
    first.close()
    second = entry._bind_instance_lock()
    assert second is not None
    second.close()


if __name__ == "__main__":
    test_second_bind_fails_while_first_holds()
    test_lock_is_reacquirable_after_release()
    print("ok")
